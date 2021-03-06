# -*- coding: utf-8 -*-
"""
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

import csv
import logging

from django.conf import settings
from django.http import HttpResponse
from django.template.defaultfilters import slugify
from django.contrib.admin.util import flatten_fieldsets
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User as DjangoUser
from django.utils.translation import ugettext_lazy as _
from django.utils.datastructures import SortedDict


from django.contrib import admin as django_admin
import mongoadmin as admin

from .models import EmailContent, User

from sparks.django.admin import languages, truncate_field


LOGGER = logging.getLogger(__name__)


# •••••••••••••••••••••••••••••••••••••••••••••••• Helpers and abstract classes


class NearlyReadOnlyAdmin(django_admin.ModelAdmin):
    """ borrowed from https://code.djangoproject.com/ticket/17295
        with enhancements from http://stackoverflow.com/a/12923739/654755 """

    def get_readonly_fields(self, request, obj=None):
        if request.user.is_superuser:
            return self.readonly_fields

        if self.declared_fieldsets:
            fields = flatten_fieldsets(self.declared_fieldsets)
        else:
            fields = [field.name for field in self.model._meta.fields]
        return fields

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser


class CSVAdminMixin(django_admin.ModelAdmin):
    """
    Adds a CSV export action to an admin view.
    Modified/updated version of http://djangosnippets.org/snippets/2908/

    http://stackoverflow.com/a/16198394/654755 could be a future cool thing
    to implement, to avoid selecting everyone before exporting.
    """

    # This is the maximum number of records that will be written.
    # Exporting massive numbers of records should be done asynchronously.
    # Excel is capped @ 65535 + 1 header line.
    csv_record_limit = 65535
    extra_csv_fields = ()

    def get_actions(self, request):

        try:
            self.actions.append('csv_export')

        except AttributeError:
            self.actions = ('csv_export', )

        actions = super(CSVAdminMixin, self).get_actions(request)

        return actions or SortedDict()

    def get_extra_csv_fields(self, request):
        return self.extra_csv_fields

    def csv_export(self, request, queryset, *args, **kwargs):
        response = HttpResponse(mimetype='text/csv')

        response['Content-Disposition'] = "attachment; filename={}.csv".format(
            slugify(self.model.__name__)
        )

        headers = list(self.list_display) + list(
            self.get_extra_csv_fields(request)
        )

        # BOM (Excel needs it to open UTF-8 file properly)
        response.write(u'\ufeff'.encode('utf8'))
        writer = csv.DictWriter(response, headers)

        # Write header.
        header_data = {}
        for name in headers:
            if hasattr(self, name) \
                    and hasattr(getattr(self, name), 'short_description'):
                header_data[name] = getattr(
                    getattr(self, name), 'short_description')

            else:
                field = self.model._meta.get_field_by_name(name)

                if field and field[0].verbose_name:
                    header_data[name] = field[0].verbose_name

                else:
                    header_data[name] = name

            header_data[name] = header_data[name].encode('utf-8', 'ignore')

        writer.writerow(header_data)

        # Write records.
        for row in queryset[:self.csv_record_limit]:
            data = {}
            for name in headers:
                if hasattr(row, name):
                    data[name] = getattr(row, name)
                elif hasattr(self, name):
                    data[name] = getattr(self, name)(row)
                else:
                    raise Exception('Unknown field: %s' % (name,))

                if callable(data[name]):
                    data[name] = data[name]()

                if isinstance(data[name], unicode):
                    data[name] = data[name].encode('utf-8', 'ignore')
                else:
                    data[name] = unicode(data[name]).encode('utf-8', 'ignore')

            writer.writerow(data)

        return response

    csv_export.short_description = _(
        u'Export selected %(verbose_name_plural)s to CSV'
    )


# ••••••••••••••••••••••••••••••••••••••••••••••• Base Django App admin classes


class OneFlowUserAdmin(UserAdmin, CSVAdminMixin):

    list_display = ('id', 'username', 'email', 'full_name_display',
                    'date_joined', 'last_login',
                    'email_announcements',
                    'is_active', 'is_staff', 'is_superuser',
                    'groups_display', )
    list_display_links = ('username', 'email', 'full_name_display', )
    list_filter = ('email_announcements',
                   'is_active', 'is_staff', 'is_superuser', )
    ordering = ('-date_joined',)
    date_hierarchy = 'date_joined'
    search_fields = ('email', 'first_name', 'last_name', )
    change_list_template = "admin/change_list_filter_sidebar.html"
    change_list_filter_template = "admin/filter_listing.html"
    # Don't display the UserProfile inline, this will conflict with the
    # post_save() signal in profiles.models. There is a race condition…
    #inlines = [] if UserProfile is None else [UserProfileInline, ]

    def groups_display(self, obj):
        return u', '.join([g.name for g in obj.groups.all()]
                          ) if obj.groups.count() else u'—'

    groups_display.short_description = _(u'Groups')

    def full_name_display(self, obj):
        return obj.get_full_name()

    full_name_display.short_description = _(u'Full name')

admin.site.register(User, OneFlowUserAdmin)
try:
    admin.site.unregister(DjangoUser)
except:
    pass


if settings.FULL_ADMIN:
    subject_fields_names = tuple(('subject_' + code)
                                 for code, lang in languages)
    subject_fields_displays = tuple((field + '_display')
                                    for field in subject_fields_names)

    class EmailContentAdmin(django_admin.ModelAdmin):
        list_display  = ('name', ) + subject_fields_displays
        search_fields = ('name', ) + subject_fields_names
        ordering      = ('name', )
        save_as       = True

    for attr, attr_name in zip(subject_fields_names,
                               subject_fields_displays):
        setattr(EmailContentAdmin, attr_name,
                truncate_field(EmailContent, attr))

    admin.site.register(EmailContent, EmailContentAdmin)
