# -*- coding: utf-8 -*-
"""

    The :class:`User` and :class:`UserManager` classes can completely and
    transparently replace the one from Django.

    We don't use the `username` attribute, but it is implemented as a
    readonly property, returning the `email`, which we use as required
    user name field.

    ____________________________________________________________________

    Copyright 2012-2014 Olivier Cortès <oc@1flow.io>

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

import logging

from transmeta import TransMeta

from django.db import models
from django.utils.http import urlquote
from django.contrib.auth.models import (BaseUserManager,
                                        AbstractBaseUser,
                                        PermissionsMixin)
from django.utils.translation import ugettext_lazy as _

from ..profiles.models import AbstractUserProfile
from ..base.utils.dateutils import now

LOGGER = logging.getLogger(__name__)


# ••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••••• Classes


class EmailContent(models.Model):
    __metaclass__ = TransMeta

    name    = models.CharField(_('Email name'),
                               max_length=128, unique=True)
    subject = models.CharField(_('Email subject'), max_length=256)
    body    = models.TextField(_('Email body'))

    def __unicode__(self):
        return _(u'{field_name}: {truncated_field_value}').format(
            field_name=self.name, truncated_field_value=self.subject[:30]
            + (self.subject[30:] and u'…'))

    class Meta:
        translate = ('subject', 'body', )
        verbose_name = _(u'Email content')
        verbose_name_plural = _(u'Emails contents')


class UserManager(BaseUserManager):
    """ This is a free adaptation of
        https://github.com/django/django/blob/master/django/contrib/auth/models.py  # NOQA
        as of 20130526. """

    def create_user(self, username, email, password=None, **extra_fields):
        """ Creates and saves a User with the given username,
            email and password. """

        now1 = now()

        if not email:
            raise ValueError('User must have an email')

        email = UserManager.normalize_email(email)

        user = self.model(username=username, email=email,
                          is_active=True, is_staff=False, is_superuser=False,
                          last_login=now1, date_joined=now1, **extra_fields)

        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password, **extra_fields):
        u = self.create_user(username, email, password, **extra_fields)
        u.is_staff = True
        u.is_active = True
        u.is_superuser = True
        u.save(using=self._db)
        return u


class User(AbstractBaseUser, PermissionsMixin, AbstractUserProfile):
    """ Username, password and email are required.
        Other fields are optional. """

    #NOTE: AbstractBaseUser brings `password` and `last_login` fields.

    username = models.CharField(_('User name'), max_length=254,
                                unique=True, db_index=True,
                                help_text=_('Required. letters, digits, '
                                            'and "@+-_".'))
    email = models.EmailField(_('email address'),  max_length=254,
                              unique=True, db_index=True,
                              help_text=_('Any valid email address.'))
    first_name = models.CharField(_('first name'), max_length=64, blank=True)
    last_name = models.CharField(_('last name'), max_length=64, blank=True)
    is_staff = models.BooleanField(_('staff status'), default=False,
                                   help_text=_('Designates whether the user '
                                               'can log into this admin '
                                               'site.'))
    is_active = models.BooleanField(_('active'), default=True,
                                    help_text=_('Designates whether this user '
                                                'should be treated as '
                                                'active. Unselect this instead '
                                                'of deleting accounts.'))
    date_joined = models.DateTimeField(_('date joined'), default=now)

    objects = UserManager()

    USERNAME_FIELD = 'username'
    REQUIRED_FIELDS = ('email', )

    class Meta:
        verbose_name = _('user')
        verbose_name_plural = _('users')

    def get_absolute_url(self):
        return _("/users/{username}/").format(urlquote(self.username))

    def get_full_name(self):
        """ Returns the first_name plus the last_name, with a space in between.
        """
        full_name = '%s %s' % (self.first_name, self.last_name)
        return full_name.strip()

    def get_short_name(self):
        "Returns the short name for the user."
        return self.username

    # NOTE: self.email_user() comes from the AbstractUserProfile class


class OwnerOrSuperuserEditAdaptor(object):
    """ This is the custom adaptor for django-inplaceedit permissions.

        It will grant edit access if the current user is superuser or
        staff and he has NOT disabled the super_powers preference, or
        if the current user is the owner of the model instance being
        edited.

        .. note:: it makes some assumptions about beiing used with
            1flow models. Eg. it assumes the user will have a `.mongo`
            attribute, and that `obj` instance will have a `.owner`
            or `.user` attribute. Besides the `.mongo` attr, the
            others are quite common in all the apps I found until today.

    """

    @classmethod
    def can_edit(cls, adaptor_field):

        user = adaptor_field.request.user
        obj = adaptor_field.obj

        if user.is_anonymous():
            return False

        elif user.mongo.is_staff_or_superuser_and_enabled:
            return True

        else:
            try:
                if isinstance(obj, models.Model):
                    # We are editing a Django model. A user can be
                    # editing his own account, or one of its objects.
                    return user == obj or user == obj.user

                else:
                    # We are editing a MongoDB Document. Either the it's
                    # user account object, or one of its own objects. In
                    # 1flow core, this is stated by either a `.owner`
                    # (which has my semantic preference) attribute, or a
                    # more classic `.user` (àla Django, used when the
                    # relation is not a real "ownership" but a simple
                    # N-N relation).
                    try:
                        return user == obj.django or user.mongo == obj.owner

                    except AttributeError:
                        # We don't try/except this one: it can crash with
                        # AttributeError for the same reason as the previous,
                        # but it will be catched by the outer try/except
                        # because we have nothing more to try, anyway.
                        return user == obj.django or user.mongo == obj.user

            except:
                LOGGER.exception(u'Exception while testing %s ownership on %s',
                                 user, obj)

            # This test is good for staff members,
            # but too weak for normal users.
            #can_edit = has_permission(obj, user, 'edit')

        return False
