# -*- coding: utf-8 -*-
"""
    Copyright 2013-2014 Olivier Cortès <oc@1flow.io>

    This file is part of the 1flow project.

    1flow is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as
    published by the Free Software Foundation, either version 3 of
    the License, or (at your option) any later version.

    1flow is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public
    License along with 1flow.  If not, see http://www.gnu.org/licenses/

"""

import redis
import logging
import time as pytime

from random import randrange
from constance import config

from pymongo.errors import DuplicateKeyError
from mongoengine.fields import DBRef
from mongoengine.errors import OperationError, NotUniqueError, ValidationError
from mongoengine.queryset import Q
from mongoengine.context_managers import no_dereference

from libgreader import GoogleReader, OAuth2Method
from libgreader.url import ReaderUrl

from celery import task, chain as tasks_chain

from django.conf import settings
from django.core.mail import mail_admins
from django.core.urlresolvers import reverse
from django.core.mail import mail_managers
from django.contrib.auth import get_user_model
from django.utils.translation import ugettext_lazy as _

from .models import (RATINGS,
                     Article,
                     Feed, feed_refresh,
                     Subscription, Read, User as MongoUser)
from .stats import synchronize_statsd_articles_gauges

from .gr_import import GoogleReaderImport

from ..base.utils import RedisExpiringLock
from ..base.utils.dateutils import (now, ftstamp, today, timedelta, datetime,
                                    naturaldelta, naturaltime, benchmark)

# We don't fetch articles too far in the past, even if google has them.
GR_OLDEST_DATE = datetime(2008, 1, 1)

LOGGER = logging.getLogger(__name__)

REDIS = redis.StrictRedis(host=settings.REDIS_HOST,
                          port=settings.REDIS_PORT,
                          db=settings.REDIS_DB)

User = get_user_model()


def get_user_from_dbs(user_id):
    django_user = User.objects.get(id=user_id)
    MongoUser.objects(django_user=django_user.id
                      ).update_one(set__django_user=django_user.id,
                                   upsert=True)

    return django_user, MongoUser.objects.get(django_user=django_user.id)


def import_google_reader_trigger(user_id, refresh=False):
    """ This function allow to trigger the celery task from anywhere.
        just pass it a user ID. It's called from the views, and we created
        it to deal whith `access_token`-related errors before launching
        the real task.

        Without doing like this, the error would happen in the remote
        celery task and we would not be aware of it in the django views.
    """

    user = User.objects.get(id=user_id)

    if refresh:
        # See http://django-social-auth.readthedocs.org/en/latest/use_cases.html#token-refreshing # NOQA
        LOGGER.warning(u'Refreshing invalid access_token for user %s.',
                       user.username)

        social = user.social_auth.get(provider='google-oauth2')
        social.refresh_token()

    # Try to get the token. If this fails, the caller will
    # catch the exception and will notify the user.
    access_token = user.social_auth.get(
        provider='google-oauth2').tokens['access_token']

    # notify the start, to instantly refresh user
    # home and admin interface lists upon reload.
    GoogleReaderImport(user_id).start(set_time=True)

    # Go to high-priority queue to allow parallel processing
    import_google_reader_begin.apply_async((user_id, access_token),
                                           queue='high')


def create_feed(gr_feed, mongo_user, subscription=True):

    try:
        Feed.objects(url=gr_feed.feedUrl,
                     site_url=gr_feed.siteUrl
                     ).update(set__url=gr_feed.feedUrl,
                              set__site_url=gr_feed.siteUrl,
                              set__name=gr_feed.title,
                              upsert=True)

    except (OperationError, DuplicateKeyError):
        LOGGER.warning(u'Duplicate feed “%s”, skipped insertion.',
                       gr_feed.title)

    feed = Feed.objects.get(url=gr_feed.feedUrl, site_url=gr_feed.siteUrl)

    if subscription:
        tags = [c.label for c in gr_feed.getCategories()]

        Subscription.objects(feed=feed,
                             user=mongo_user
                             ).update(set__feed=feed,
                                      set__user=mongo_user,
                                      set__tags=tags,
                                      upsert=True)

    return feed


def create_article_and_read(article_url,
                            article_title, article_content,
                            article_time, article_data,
                            feed, mongo_users, is_read, is_starred,
                            categories):
    #LOGGER.info(u'Importing article “%s” from feed “%s” (%s/%s, wave %s)…',
    #     gr_article.title, gr_feed.title, articles_counter, total, wave + 1)

    try:
        Article.objects(url=article_url).update_one(
            set__url=article_url, set__title=article_title,
            add_to_set__feeds=feed, set__content=article_content,
            set__date_published=None
            if article_time is None
            else ftstamp(article_time),
            set__google_reader_original_data=article_data, upsert=True)

    except (OperationError, DuplicateKeyError):
        LOGGER.warning(u'Duplicate article “%s” (url: %s) in feed “%s”: '
                       u'DATA=%s', article_title, article_url, feed.name,
                       article_data)

    try:
        article = Article.objects.get(url=article_url)

    except Article.DoesNotExist:
        LOGGER.error(u'Article “%s” (url: %s) in feed “%s” upsert failed: '
                     u'DATA=%s', article_title[:40]
                     + (article_title[:40] and u'…'),
                     article_url[:40] + (article_url[:40] and u'…'),
                     feed.name, article_data)
        return

    for mongo_user in mongo_users:
        Read.objects(article=article,
                     user=mongo_user
                     ).update(set__article=article,
                              set__user=mongo_user,
                              set__tags=categories,
                              set__is_read=is_read,
                              set__rating=article.default_rating +
                              (RATINGS.STARRED if is_starred
                               else 0.0),
                              upsert=True)


def end_fetch_prematurely(kind, gri, processed, gr_article, gr_feed_title,
                          username, hard_articles_limit, date_limit=None):

    if not gri.running():
        # Everything is stopped. Just return. Either the admin
        # stopped manually, or we reached a hard limit.
        gri.incr_feeds()
        return True

    if kind == 'reads' and gri.reads() >= gri.total_reads():
        LOGGER.info(u'All read articles imported for user %s.', username)
        gri.incr_feeds()
        return True

    if kind == 'starred' and gri.starred() >= gri.total_starred():
        LOGGER.info(u'All starred articles imported for user %s.', username)
        gri.incr_feeds()
        return True

    if gri.articles() >= hard_articles_limit:
        LOGGER.info(u'Reached hard storage limit for user %s, stopping '
                    u'import of feed “%s”.', username, gr_feed_title)
        gri.incr_feeds()
        return True

    if date_limit:
        if gr_article.time is None:
            LOGGER.warning(u'Article %s (feed “%s”, user %s) has no time. '
                           u'DATA=%s.', gr_article, gr_feed_title,
                           username, gr_article.data)

        else:
            if ftstamp(gr_article.time) < date_limit:
                LOGGER.warning(u'Datetime limit reached on feed “%s” '
                               u'for user %s, stopping. %s article(s) '
                               u'imported.', gr_feed_title,
                               username, processed)
                # We could (or not) have reached all the read items
                # in this feed. In case we don't, incr() feeds will
                # ensure the whole import will stop if all feeds
                # reach their date limit (or end by lack of items).
                gri.incr_feeds()
                return True

    return False


def end_task_or_continue_fetching(gri, total, wave,
                                  gr_feed_title, username,
                                  task_to_call, task_args, task_kwargs):

    if not gri.running():
        gri.incr_feeds()
        return True

    # We use ">" (not just "==") in case the limit was lowered
    # by the administrators during the current run.
    if total >= config.GR_LOAD_LIMIT:
        # Reaching the load limit means “Go for next wave”, because
        # there is probably more articles. We have to fetch to see.

        if wave < config.GR_WAVE_LIMIT:
            task_to_call.delay(*task_args, **task_kwargs)

            LOGGER.info(u'Wave %s imported %s articles of '
                        u'feed “%s” for user %s, continuing.', wave, total,
                        gr_feed_title, username)

        else:
            LOGGER.warning(u'Wave limit reached on feed “%s”, for user '
                           u'%s, stopping.', gr_feed_title, username)
            # Notify we are out of luck.
            gri.incr_feeds()
            return True
    else:
        # We have reached the "beginning" of the feed in GR.
        # Probably one with only a few subscribers, because
        # in normal conditions, GR data is virtually unlimited.
        LOGGER.info(u'Reached beginning of feed “%s” for user %s, %s '
                    u'article(s) imported.', gr_feed_title, username, total)
        gri.incr_feeds()
        return True

    return False


def empty_gr_feed(gr_feed):
    """ Remove processed items from the feed.

        Having removed them here adds the benefit of
        not storing them in the celery queue :-)
    """

    # Remove the read items before loading more
    # else we leak much too much too muuuuuch…
    gr_feed.items[:] = []

    # Avoid keeping strong references too…
    gr_feed.itemsById = {}


@task
def import_google_reader_begin(user_id, access_token):

    auth = OAuth2Method(settings.GOOGLE_OAUTH2_CLIENT_ID,
                        settings.GOOGLE_OAUTH2_CLIENT_SECRET)
    auth.authFromAccessToken(access_token)
    reader = GoogleReader(auth)

    django_user, mongo_user = get_user_from_dbs(user_id)
    username = django_user.username

    try:
        user_infos = reader.getUserInfo()

    except TypeError:
        LOGGER.exception(u'Could not start Google Reader import for user %s.',
                         username)
        # Don't refresh, it's now done by a dedicated periodic task.
        # If we failed, it means the problem is quite serious.
        #       import_google_reader_trigger(user_id, refresh=True)
        return

    GR_MAX_FEEDS = config.GR_MAX_FEEDS

    LOGGER.info(u'Starting Google Reader import for user %s.', username)

    gri = GoogleReaderImport(user_id)

    # take note of user informations now that we have them.
    gri.start(user_infos=user_infos)

    reader.buildSubscriptionList()

    total_reads, reg_date     = reader.totalReadItems(without_date=False)
    total_starred, star1_date = reader.totalStarredItems(without_date=False)
    total_feeds               = len(reader.feeds) + 1  # +1 for 'starred'

    gri.reg_date(pytime.mktime(reg_date.timetuple()))
    gri.star1_date(pytime.mktime(star1_date.timetuple()))
    gri.total_reads(total_reads)
    gri.total_starred(total_starred)

    LOGGER.info(u'Google Reader import for user %s: %s feed(s) and %s read '
                u'article(s) to go…', username, total_feeds, total_reads)

    if total_feeds > GR_MAX_FEEDS and not settings.DEBUG:
        mail_admins('User {0} has more than {1} feeds: {2}!'.format(
                    username, GR_MAX_FEEDS, total_feeds),
                    u"\n\nThe GR import will be incomplete.\n\n"
                    u"Just for you to know…\n\n")

    # We launch the starred feed import first. Launching it after the
    # standard feeds makes it being delayed until the world's end.
    reader.makeSpecialFeeds()
    starred_feed = reader.getSpecialFeed(ReaderUrl.STARRED_LIST)
    import_google_reader_starred.apply_async((user_id, username, starred_feed),
                                             queue='low')

    processed_feeds = 1
    feeds_to_import = []

    for gr_feed in reader.feeds[:GR_MAX_FEEDS]:

        try:
            feed = create_feed(gr_feed, mongo_user)

        except Feed.DoesNotExist:
            LOGGER.exception(u'Could not create feed “%s” for user %s, '
                             u'skipped.', gr_feed.title, username)
            continue

        processed_feeds += 1
        feeds_to_import.append((user_id, username, gr_feed, feed))

        LOGGER.info(u'Imported feed “%s” (%s/%s) for user %s…',
                    gr_feed.title, processed_feeds, total_feeds, username)

    # We need to clamp the total, else task won't finish in
    # the case where the user has more feeds than allowed.
    #
    gri.total_feeds(min(processed_feeds, GR_MAX_FEEDS))

    for feed_args in feeds_to_import:
        import_google_reader_articles.apply_async(feed_args, queue='low')

    LOGGER.info(u'Imported %s/%s feeds in %s. Articles import already '
                u'started with limits: date: %s, %s waves of %s articles, '
                u'max articles: %s, reads: %s, starred: %s.',
                processed_feeds, total_feeds,
                naturaldelta(now() - gri.start()),
                naturaltime(max([gri.reg_date(), GR_OLDEST_DATE])),
                config.GR_WAVE_LIMIT, config.GR_LOAD_LIMIT,
                config.GR_MAX_ARTICLES, total_reads, total_starred)


@task
def import_google_reader_starred(user_id, username, gr_feed, wave=0):

    mongo_user = MongoUser.objects.get(django_user=user_id)
    gri        = GoogleReaderImport(user_id)
    date_limit = max([gri.reg_date(), GR_OLDEST_DATE])
    hard_limit = config.GR_MAX_ARTICLES

    if not gri.running():
        # In case the import was stopped while this task was stuck in the
        # queue, just stop now. As we return, it's one more feed processed.
        gri.incr_feeds()
        return

    load_method = gr_feed.loadItems if wave == 0 else gr_feed.loadMoreItems

    retry = 0

    while True:
        try:
            load_method(loadLimit=config.GR_LOAD_LIMIT)

        except:
            if retry < config.GR_MAX_RETRIES:
                LOGGER.warning(u'Wave %s (try %s) of feed “%s” failed to load '
                               u'for user %s, retrying…', wave, retry,
                               gr_feed.title, username)
                pytime.sleep(5)
                retry += 1

            else:
                LOGGER.exception(u'Wave %s of feed “%s” failed to load for '
                                 u'user %s, after %s retries, aborted.', wave,
                                 gr_feed.title, username, retry)
                gri.incr_feeds()
                return
        else:
            break

    total            = len(gr_feed.items)
    articles_counter = 0

    for gr_article in gr_feed.items:
        if end_fetch_prematurely('starred', gri, articles_counter,
                                 gr_article, gr_feed.title, username,
                                 hard_limit, date_limit=date_limit):
            return

        # Get the origin feed, the "real" one.
        # because currently gr_feed = 'starred',
        # which is a virtual one without an URL.
        real_gr_feed = gr_article.feed
        subscribed   = real_gr_feed in gr_feed.googleReader.feeds

        try:
            feed = create_feed(real_gr_feed, mongo_user,
                               subscription=subscribed)

        except Feed.DoesNotExist:
            LOGGER.exception(u'Could not create feed “%s” (from starred) for '
                             u'user %s, skipped.', real_gr_feed.title, username)

            # We increment anyway, else the import will not finish.
            # TODO: We should decrease the starred total instead.
            gri.incr_starred()
            continue

        create_article_and_read(gr_article.url, gr_article.title,
                                gr_article.content,
                                gr_article.time, gr_article.data,
                                feed, [mongo_user], gr_article.read,
                                gr_article.starred,
                                [c.label for c
                                 in real_gr_feed.getCategories()])

        if not subscribed:
            gri.incr_articles()

        # starred don't implement reads. Not perfect, not
        # accurate, but no time for better implementation.
        #if gr_article.read:
        #    gri.incr_reads()
        gri.incr_starred()

    empty_gr_feed(gr_feed)

    end_task_or_continue_fetching(gri, total, wave,
                                  gr_feed.title, username,
                                  import_google_reader_starred,
                                  (user_id, username, gr_feed),
                                  {'wave': wave + 1})


@task
def import_google_reader_articles(user_id, username, gr_feed, feed, wave=0):

    mongo_user = MongoUser.objects.get(django_user=user_id)
    gri        = GoogleReaderImport(user_id)
    date_limit = max([gri.reg_date(), GR_OLDEST_DATE])
    # Be sure all starred articles can be fetched.
    hard_limit = config.GR_MAX_ARTICLES - gri.total_starred()

    if not gri.running():
        # In case the import was stopped while this task was stuck in the
        # queue, just stop now. As we return, it's one more feed processed.
        gri.incr_feeds()
        return

    load_method = gr_feed.loadItems if wave == 0 else gr_feed.loadMoreItems

    retry = 0

    while True:
        try:
            load_method(loadLimit=config.GR_LOAD_LIMIT)

        except:
            if retry < config.GR_MAX_RETRIES:
                LOGGER.warning(u'Wave %s (try %s) of feed “%s” failed to load '
                               u'for user %s, retrying…', wave, retry,
                               gr_feed.title, username)
                pytime.sleep(5)
                retry += 1

            else:
                LOGGER.exception(u'Wave %s of feed “%s” failed to load for '
                                 u'user %s, after %s retries, aborted.', wave,
                                 gr_feed.title, username, retry)
                gri.incr_feeds()
                return
        else:
            break

    total            = len(gr_feed.items)
    articles_counter = 0

    categories = [c.label for c in gr_feed.getCategories()]

    for gr_article in gr_feed.items:

        if end_fetch_prematurely('reads', gri, articles_counter,
                                 gr_article, gr_feed.title, username,
                                 hard_limit, date_limit=date_limit):
            return

        # Read articles are always imported
        if gr_article.read:
            create_article_and_read(gr_article.url, gr_article.title,
                                    gr_article.content,
                                    gr_article.time, gr_article.data,
                                    feed, [mongo_user], gr_article.read,
                                    gr_article.starred,
                                    categories)
            gri.incr_articles()
            gri.incr_reads()

        # Unread articles are imported only if there is room for them.
        elif gri.articles() < (hard_limit - gri.reads()):
            create_article_and_read(gr_article.url, gr_article.title,
                                    gr_article.content,
                                    gr_article.time, gr_article.data,
                                    feed, [mongo_user], gr_article.read,
                                    gr_article.starred,
                                    categories)
            gri.incr_articles()

        articles_counter += 1

    empty_gr_feed(gr_feed)

    end_task_or_continue_fetching(gri, total, wave,
                                  gr_feed.title, username,
                                  import_google_reader_articles,
                                  (user_id, username, gr_feed, feed),
                                  {'wave': wave + 1})


def clean_gri_keys():
    """ Remove all GRI obsolete keys. """

    keys = REDIS.keys('gri:*:run')

    users_ids = [x[0] for x in User.objects.all().values_list('id')]

    for key in keys:
        user_id = int(key.split(':')[1])

        if user_id in users_ids:
            continue

        name = u'gri:%s:*' % user_id
        LOGGER.info(u'Deleting obsolete redis keys %s…' % name)
        names = REDIS.keys(name)
        REDIS.delete(*names)


@task(queue='clean')
def clean_obsolete_redis_keys():
    """ Call in turn all redis-related cleaners. """

    start_time = pytime.time()

    if today() <= (config.GR_END_DATE + timedelta(days=1)):
        clean_gri_keys()

    LOGGER.info(u'clean_obsolete_redis_keys(): finished in %s.',
                naturaldelta(pytime.time() - start_time))

# •••••••••••••••••••••••••••••••••••••••••••••••••••••••••• Refresh RSS feeds


@task(queue='high')
def refresh_all_feeds(limit=None, force=False):

    if config.FEED_FETCH_DISABLED:
        # Do not raise any .retry(), this is a scheduled task.
        LOGGER.warning(u'Feed refresh disabled in configuration.')
        return

    # Be sure two refresh operations don't overlap, but don't hold the
    # lock too long if something goes wrong. In production conditions
    # as of 20130812, refreshing all feeds takes only a moment:
    # [2013-08-12 09:07:02,028: INFO/MainProcess] Task
    #       oneflow.core.tasks.refresh_all_feeds succeeded in 1.99886608124s.
    my_lock = RedisExpiringLock('refresh_all_feeds', expire_time=120)

    if not my_lock.acquire():
        if force:
            my_lock.release()
            my_lock.acquire()
            LOGGER.warning(_(u'Forcing all feed refresh…'))

        else:
            # Avoid running this task over and over again in the queue
            # if the previous instance did not yet terminate. Happens
            # when scheduled task runs too quickly.
            LOGGER.warning(u'refresh_all_feeds() is already locked, aborting.')
            return

    feeds = Feed.objects.filter(closed__ne=True, is_internal__ne=True)

    if limit:
        feeds = feeds.limit(limit)

    # No need for caching and cluttering CPU/memory for a one-shot thing.
    feeds.no_cache()

    with benchmark('refresh_all_feeds()'):

        try:
            count = 0
            mynow = now()

            for feed in feeds:

                if feed.refresh_lock.is_locked():
                    LOGGER.info(u'Feed %s already locked, skipped.', feed)
                    continue

                interval = timedelta(seconds=feed.fetch_interval)

                if feed.last_fetch is None:

                    feed_refresh.delay(feed.id)

                    LOGGER.info(u'Launched immediate refresh of feed %s which '
                                u'has never been refreshed.', feed)

                elif force or feed.last_fetch + interval < mynow:

                    how_late = feed.last_fetch + interval - mynow
                    how_late = how_late.days * 86400 + how_late.seconds

                    if config.FEED_REFRESH_RANDOMIZE:
                        countdown = randrange(
                            config.FEED_REFRESH_RANDOMIZE_DELAY)

                        feed_refresh.apply_async((feed.id, force),
                                                 countdown=countdown)

                    else:
                        countdown = 0
                        feed_refresh.delay(feed.id, force)

                    LOGGER.info(u'%s refresh of feed %s %s (%s late).',
                                u'Scheduled randomized'
                                if countdown else u'Launched',
                                feed,
                                u' in {0}'.format(naturaldelta(countdown))
                                if countdown else u'in the background',
                                naturaldelta(how_late))
                    count += 1

        finally:
            my_lock.release()

        LOGGER.info(u'Launched %s refreshes out of %s feed(s) checked.',
                    count, feeds.count())


@task(queue='high')
def global_checker_task(*args, **kwargs):
    """ Just run all tasks in a celery chain, to avoid
        them to overlap and hit the database too much. """

    global_check_chain = tasks_chain(
        # HEADS UP: all subtasks are immutable, we just want them to run
        # chained to avoid dead times, without any link between them.

        # Begin by checking duplicates and archiving as much as we can,
        # for next tasks to work on the smallest-possible objects set.
        global_duplicates_checker.si(),
        archive_documents.si(),

        global_subscriptions_checker.si(),
        global_reads_checker.si(),
    )

    return global_check_chain.delay()


@task(queue='low')
def global_feeds_checker():

    def pretty_print_feed(feed):

        return (u'- %s,\n'
                u'    - admin url: http://%s%s\n'
                u'    - public url: %s\n'
                u'    - %s\n'
                u'    - reason: %s\n'
                u'    - last error: %s') % (
                feed,

                settings.SITE_DOMAIN,

                reverse('admin:%s_%s_change' % (
                    feed._meta.get('app_label', 'nonrel'),
                    feed._meta.get('module_name', 'feed')),
                    args=[feed.id]),

                feed.url,

                (u'closed on %s' % feed.date_closed)
                    if feed.date_closed
                    else u'(no closing date)',

                feed.closed_reason or
                u'none (or manually closed from the admin interface)',

                feed.errors[0]
                    if len(feed.errors)
                    else u'(no error recorded)')

    dtnow        = now()
    limit_days   = config.FEED_CLOSED_WARN_LIMIT
    closed_limit = dtnow - timedelta(days=limit_days)

    feeds = Feed.objects(Q(closed=True)
                         & (Q(date_closed__exists=False)
                            | Q(date_closed__gte=closed_limit)))

    count = feeds.count()

    if count > 10000:
        # prevent CPU and memory hogging.
        LOGGER.info(u'Switching query to no_cache(), this will take longer.')
        feeds.no_cache()

    if not count:
        LOGGER.info('No feed was closed in the last %s days.', limit_days)
        return

    mail_managers(_(u'Reminder: {0} feed(s) closed in last '
                  u'{1} day(s)').format(count, limit_days),
                  _(u"\n\nHere is the list, dates (if any), and reasons "
                    u"(if any) of closing:\n\n{feed_list}\n\nYou can manually "
                    u"reopen any of them from the admin interface.\n\n").format(
                          feed_list='\n\n'.join(pretty_print_feed(feed)
                                                for feed in feeds)))

    start_time = pytime.time()

    # Close the feeds, but after sending the mail,
    # So that initial reason is displayed at least
    # once to a real human.
    for feed in feeds:
        if feed.date_closed is None:
            feed.close('Automatic close by periodic checker task')

    LOGGER.info('Closed %s feeds in %s.', count,
                naturaldelta(pytime.time() - start_time))


@task(queue='low')
def global_subscriptions_checker(force=False, limit=None, from_feeds=True,
                                 from_users=False, extended_check=False):
    """ A conditionned version of :meth:`Feed.check_subscriptions`. """

    if config.CHECK_SUBSCRIPTIONS_DISABLED:
        LOGGER.warning(u'Subscriptions checks disabled in configuration.')
        return

    # This task runs one a day. Acquire the lock for just a
    # little more time to avoid over-parallelized runs.
    my_lock = RedisExpiringLock('check_all_subscriptions',
                                expire_time=3600 * 25)

    if not my_lock.acquire():
        if force:
            my_lock.release()
            my_lock.acquire()
            LOGGER.warning(u'Forcing subscriptions checks…')

        else:
            # Avoid running this task over and over again in the queue
            # if the previous instance did not yet terminate. Happens
            # when scheduled task runs too quickly.
            LOGGER.warning(u'global_subscriptions_checker() is already '
                           u'locked, aborting.')
            return

    if limit is None:
        limit = 0

    assert int(limit) >= 0

    try:
        if from_feeds:
            with benchmark("Check all subscriptions from feeds"):

                feeds           = Feed.good_feeds.no_cache()
                feeds_count     = feeds.count()
                processed_count = 0
                checked_count   = 0

                for feed in feeds:

                    if limit and checked_count > limit:
                        break

                    if extended_check:
                        feed.compute_cached_descriptors(all=True,
                                                        good=True,
                                                        bad=True)

                    # Do not extended_check=True, this would double-do
                    # the subscription.check_reads() already called below.
                    feed.check_subscriptions()

                    for subscription in feed.subscriptions:

                        processed_count += 1

                        if subscription.all_articles_count \
                                != feed.good_articles_count:

                            checked_count += 1

                            LOGGER.info(u'Subscription %s (#%s) has %s reads '
                                        u'whereas its feed has %s good '
                                        u'articles; checking…',
                                        subscription.name, subscription.id,
                                        subscription.all_articles_count,
                                        feed.good_articles_count)

                            subscription.check_reads(
                                force=True, extended_check=extended_check)

                LOGGER.info(u'%s/%s (limit:%s) feeds processed, %s '
                            u'checked (%.2f%%).',
                            processed_count, feeds_count, limit,
                            checked_count, checked_count
                            * 100.0 / processed_count)

        if from_users:
            with benchmark("Check all subscriptions from users"):

                users           = MongoUser.objects.all().no_cache()
                users_count     = users.count()
                processed_count = 0

                for user in users:

                    # Do not extended_check=True, this would double-do
                    # the subscription.check_reads() already called below.
                    user.check_subscriptions()

                    if extended_check:
                        user.compute_cached_descriptors(all=True,
                                                        unread=True,
                                                        starred=True,
                                                        bookmarked=True)

                        for subscription in user.subscriptions:
                                processed_count += 1

                                subscription.check_reads(force=True,
                                                         extended_check=True)

                LOGGER.info(u'%s users %sprocessed. '
                            u'All were checked.', users_count,
                            u'and %s subscriptions '.format(processed_count)
                            if extended_check else u'')

    finally:
        my_lock.release()


@task(queue='low')
def global_duplicates_checker(limit=None, force=False):
    """ Something that will ensure that duplicate articles have no more read
        anywhere, fix it if not, and update all counters accordingly. """

    if config.CHECK_DUPLICATES_DISABLED:
        LOGGER.warning(u'Duplicates check disabled in configuration.')
        return

    # This task runs one a day. Acquire the lock for just a
    # little more time to avoid over-parallelized runs.
    my_lock = RedisExpiringLock('check_all_duplicates', expire_time=3600 * 25)

    if not my_lock.acquire():
        if force:
            my_lock.release()
            my_lock.acquire()
            LOGGER.warning(u'Forcing duplicates check…')

        else:
            # Avoid running this task over and over again in the queue
            # if the previous instance did not yet terminate. Happens
            # when scheduled task runs too quickly.
            LOGGER.warning(u'global_subscriptions_checker() is already '
                           u'locked, aborting.')
            return

    if limit is None:
        limit = 0

    duplicates = Article.objects(duplicate_of__ne=None).no_cache()

    total_dupes_count = duplicates.count()
    total_reads_count = 0
    processed_dupes   = 0
    done_dupes_count  = 0

    with benchmark(u"Check {0}/{1} duplicates".format(limit,
                   total_dupes_count)):

        try:
            for duplicate in duplicates:
                reads = Read.objects(article=duplicate)

                processed_dupes += 1

                if reads:
                    done_dupes_count  += 1
                    reads_count        = reads.count()
                    total_reads_count += reads_count

                    LOGGER.info(u'Duplicate article %s still has %s '
                                u'reads, fixing…', duplicate, reads_count)

                    duplicate.duplicate_of.replace_duplicate_everywhere(
                        duplicate)

                    if limit and done_dupes_count >= limit:
                        break

        finally:
            my_lock.release()

    LOGGER.info(u'global_duplicates_checker(): %s/%s duplicates processed '
                u'(%.2f%%), %s corrected (%.2f%%), %s reads altered.',
                processed_dupes, total_dupes_count,
                processed_dupes * 100.0 / total_dupes_count,
                done_dupes_count, done_dupes_count * 100.0 / processed_dupes,
                total_reads_count)


@task(queue='low')
def global_reads_checker(limit=None, force=False, verbose=False,
                         break_on_exception=False, extended_check=False):

    if config.CHECK_READS_DISABLED:
        LOGGER.warning(u'Reads check disabled in configuration.')
        return

    # This task runs twice a day. Acquire the lock for just a
    # little more time (13h, because Redis doesn't like floats)
    # to avoid over-parallelized runs.
    my_lock = RedisExpiringLock('check_all_reads', expire_time=3600 * 13)

    if not my_lock.acquire():
        if force:
            my_lock.release()
            my_lock.acquire()
            LOGGER.warning(u'Forcing reads check…')

        else:
            # Avoid running this task over and over again in the queue
            # if the previous instance did not yet terminate. Happens
            # when scheduled task runs too quickly.
            LOGGER.warning(u'global_reads_checker() is already '
                           u'locked, aborting.')
            return

    if limit is None:
        limit = 0

    bad_reads = Read.objects(Q(is_good__exists=False)
                             | Q(is_good__ne=True)).no_cache()

    total_reads_count   = bad_reads.count()
    processed_reads     = 0
    wiped_reads_count   = 0
    changed_reads_count = 0
    skipped_count       = 0

    with benchmark(u"Check {0}/{1} reads".format(limit, total_reads_count)):
        try:
            for read in bad_reads:

                processed_reads += 1

                if limit and changed_reads_count >= limit:
                    break

                if read.is_good:
                    # This read has been activated via another
                    # one, attached to the same article.
                    changed_reads_count += 1
                    continue

                article = read.article

                if isinstance(article, DBRef) or article is None:
                    # The article doesn't exist anymore. Wipe the read.
                    wiped_reads_count += 1
                    LOGGER.error(u'Read #%s has dangling reference to '
                                 u'non-existing article #%s, removing.',
                                 read.id, article.id if article else u'`None`')
                    read.delete()
                    continue

                if extended_check:
                    try:
                        if read.subscriptions:

                            # TODO: remove this
                            #       check_set_subscriptions_131004_done
                            #       transient check.
                            if read.check_set_subscriptions_131004_done:
                                read.check_subscriptions()

                            else:
                                read.check_set_subscriptions_131004()
                                read.safe_reload()

                        else:
                            read.set_subscriptions()

                    except:
                        skipped_count += 1
                        LOGGER.exception(u'Could not set subscriptions on '
                                         u'read #%s, from article #%s, for '
                                         u'user #%s. Skipping.', read.id,
                                         article.id, read.user.id)
                        continue

                try:
                    if article.is_good:
                        changed_reads_count += 1

                        if verbose:
                            LOGGER.info(u'Bad read %s has a good article, '
                                        u'fixing…', read)

                        article.activate_reads(extended_check=extended_check)

                except:
                    LOGGER.exception(u'Could not activate reads from '
                                     u'article %s of read %s.',
                                     article, read)
                    if break_on_exception:
                        break

        finally:
            my_lock.release()

    LOGGER.info(u'global_reads_checker(): %s/%s reads processed '
                u'(%.2f%%), %s corrected (%.2f%%), %s deleted (%.2f%%), '
                u'%s skipped (%.2f%%).',
                processed_reads, total_reads_count,
                processed_reads * 100.0 / total_reads_count,
                changed_reads_count,
                changed_reads_count * 100.0 / processed_reads,
                wiped_reads_count,
                wiped_reads_count * 100.0 / processed_reads,
                skipped_count,
                skipped_count * 100.0 / processed_reads)


# ••••••••••••••••••••••••••••••••••••••••••••••••••• Move things to Archive DB


def archive_article_one_internal(article, counts):
    """ internal function. Do not use directly
        unless you know what you're doing. """

    delete_anyway = True
    article.switch_db('archive')

    try:
        article.save()

    except (NotUniqueError, DuplicateKeyError):
        counts['archived_dupes'] += 1

    except ValidationError:
        # If the article doesn't validate in the archive database, how
        # the hell did it make its way into the production one?? Perhaps
        # a scoria of the GR import which did update_one(set_*…), which
        # bypassed the validation phase.
        # Anyway, beiing here means the article is duplicate or orphaned.
        # So just forget the validation error and wipe it from production.
        LOGGER.exception(u'Article archiving failed for %s', article)
        counts['bad_articles'] += 1

    except:
        delete_anyway = False

    if delete_anyway:
        article.switch_db('default')
        article.delete()


@task(queue='clean')
def archive_articles(limit=None):

    counts = {
        'duplicates': 0,
        'orphaned': 0,
        'bad_articles': 0,
        'archived_dupes': 0,
    }

    if limit is None:
        limit = config.ARTICLE_ARCHIVE_BATCH_SIZE

    with no_dereference(Article) as ArticleOnly:
        if config.ARTICLE_ARCHIVE_OLDER_THAN > 0:
            older_than = now() - timedelta(
                            days=config.ARTICLE_ARCHIVE_OLDER_THAN)

            duplicates = ArticleOnly.objects(
                            duplicate_of__ne=None,
                            date_published__lt=older_than).limit(limit)
            orphaned   = ArticleOnly.objects(
                            orphaned=True,
                            date_published__lt=older_than).limit(limit)

        else:
            duplicates = ArticleOnly.objects(duplicate_of__ne=None
                                             ).limit(limit)
            orphaned   = ArticleOnly.objects(orphaned=True).limit(limit)

    duplicates.no_cache()
    orphaned.no_cache()

    counts['duplicates'] = duplicates.count()
    counts['orphaned']   = orphaned.count()

    if counts['duplicates']:
        current = 0
        LOGGER.info(u'Archiving of %s duplicate article(s) started.',
                    counts['duplicates'])

        with benchmark('Archiving of %s duplicate article(s)'
                       % counts['duplicates']):
            for article in duplicates:
                archive_article_one_internal(article, counts)
                current += 1
                if current % 50 == 0:
                    LOGGER.info(u'Archived %s/%s duplicate articles so far.',
                                current, counts['duplicates'])

    if counts['orphaned']:
        current = 0
        LOGGER.info(u'Archiving of %s orphaned article(s) started.',
                    counts['orphaned'])

        with benchmark('Archiving of %s orphaned article(s)'
                       % counts['orphaned']):
            for article in orphaned:
                archive_article_one_internal(article, counts)
                current += 1
                if current % 50 == 0:
                    LOGGER.info(u'Archived %s/%s orphaned articles so far.',
                                current, counts['duplicates'])

    if counts['duplicates'] or counts['orphaned']:
        synchronize_statsd_articles_gauges(full=True)

        LOGGER.info('%s already archived and %s bad articles were found '
                    u'during the operation.', counts['archived_dupes'],
                    counts['bad_articles'])

    else:
        LOGGER.info(u'No article to archive.')


@task(queue='clean')
def archive_documents(limit=None, force=False):

    if config.DOCUMENTS_ARCHIVING_DISABLED:
        # Do not raise any .retry(), this is a scheduled task.
        LOGGER.warning(u'Document archiving disabled in configuration.')
        return

    # Be sure two archiving operations don't overlap, this is a very costly
    # operation for the database, and it can make the system very slugish.
    # The whole operation can be very long, we lock for a long time.
    my_lock = RedisExpiringLock('archive_documents', expire_time=3600 * 24)

    if not my_lock.acquire():
        if force:
            my_lock.release()
            my_lock.acquire()
            LOGGER.warning(u'archive_documents() force unlock/re-acquire, '
                           u'be careful with that.')

        else:
            LOGGER.warning(u'archive_documents() is already locked, aborting.')
            return

    # these are tasks, but we run them sequentially in this global archive job
    # to avoid hammering the production database with multiple archive jobs.
    archive_articles(limit=limit)

    my_lock.release()
