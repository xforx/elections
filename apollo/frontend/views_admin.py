import base64
from flask import flash, request
from flask.ext.admin import form
from flask.ext.admin.actions import action
from flask.ext.admin.contrib.mongoengine import ModelView
from flask.ext.admin.contrib.mongoengine.form import CustomModelConverter
from flask.ext.admin.form import rules
from flask.ext.admin.model.form import converts
from flask.ext.babel import lazy_gettext as _
from flask.ext.mongoengine.wtf import orm
from flask.ext.security import current_user
from flask.ext.security.utils import encrypt_password
from jinja2 import contextfunction
import magic
import pytz
from wtforms import FileField, PasswordField, SelectMultipleField
from apollo.core import admin
from apollo import models, settings
from apollo.frontend import forms


app_time_zone = pytz.timezone(settings.TIME_ZONE)
utc_time_zone = pytz.utc

DATETIME_FORMAT_SPEC = u'%Y-%m-%d %H:%M:%S %Z'

try:
    string_type = unicode
except NameError:
    string_type = str


class DeploymentModelConverter(CustomModelConverter):
    @converts('ParticipantPropertyName')
    def conv_PropertyField(self, model, field, kwargs):
        return orm.ModelConverter.conv_String(self, model, field, kwargs)


class BaseAdminView(ModelView):
    def is_accessible(self):
        '''For checking if the admin view is accessible.'''
        role = models.Role.objects.get(name='admin')
        return current_user.is_authenticated() and current_user.has_role(role)


class DeploymentAdminView(BaseAdminView):
    can_create = False
    can_delete = False
    column_list = ('name',)
    form_rules = [
        rules.FieldSet(
            (
                'name', 'logo', 'allow_observer_submission_edit',
                'dashboard_full_locations', 'hostnames',
                'participant_extra_fields'
            ),
            _('Deployment')
        )
    ]
    form_subdocuments = {
        'participant_extra_fields': {
            'form_subdocuments': {
                None: {
                    'form_columns': ('name', 'label', 'listview_visibility',)
                }
            }
        }
    }
    form_excluded_columns = ['administrative_divisions_graph',
                             'is_initialized', 'include_rejected_in_votes']
    model_form_converter = DeploymentModelConverter

    def get_query(self):
        user = current_user._get_current_object()
        return models.Deployment.objects(pk=user.deployment.pk)

    def on_model_change(self, form, model, is_created):
        raw_data = request.files[form.logo.name].read()
        if len(raw_data) == 0:
            # hack because at this point, model has already been populated
            model_copy = models.Deployment.objects.get(pk=model.pk)
            model.logo = model_copy.logo
        mimetype = magic.from_buffer(raw_data, mime=True)

        # if we don't have a valid image, don't do anything
        if not mimetype.startswith('image'):
            return

        encoded = base64.b64encode(raw_data)
        model.logo = 'data:{};base64,{}'.format(mimetype, encoded)

    def scaffold_form(self):
        form_class = super(DeploymentAdminView, self).scaffold_form()
        form_class.logo = FileField(_('Logo'))

        return form_class


class EventAdminView(BaseAdminView):
    # disallow event creation
    # can_create = False

    # what fields to be displayed in the list view
    column_list = ('name', 'start_date', 'end_date')

    # what fields filtering is allowed by
    column_filters = ('name', 'start_date', 'end_date')

    # rules for form editing. in this case, only the listed fields
    # and the header for the field set
    form_rules = [
        rules.FieldSet(('name', 'start_date', 'end_date', 'roles'), _('Event'))
    ]

    form_excluded_columns = (u'deployment')

    def get_one(self, pk):
        event = super(EventAdminView, self).get_one(pk)

        # setup permissions list
        try:
            entities = models.Need.objects.get(
                action='access_event', items=event).entities
        except models.Need.DoesNotExist:
            entities = []
        event.roles = [unicode(i.pk) for i in entities]

        # convert start and end dates to app time zone
        event.start_date = utc_time_zone.localize(event.start_date).astimezone(
            app_time_zone)
        event.end_date = utc_time_zone.localize(event.end_date).astimezone(
            app_time_zone)
        return event

    @contextfunction
    def get_list_value(self, context, model, name):
        if name in ['start_date', 'end_date']:
            original = getattr(model, name, None)
            if original:
                return utc_time_zone.localize(original).astimezone(
                    app_time_zone).strftime(DATETIME_FORMAT_SPEC)

            return original

        return super(EventAdminView, self).get_list_value(context, model, name)

    def get_query(self):
        '''Returns the queryset of the objects to list.'''
        user = current_user._get_current_object()
        return models.Event.objects(deployment=user.deployment)

    def on_model_change(self, form, model, is_created):
        # if we're creating a new event, make sure to set the
        # deployment, since it won't appear in the form
        if is_created:
            model.deployment = current_user.deployment

        # also, convert the time zone to UTC
        model.start_date = app_time_zone.localize(model.start_date).astimezone(
            utc_time_zone)
        model.end_date = app_time_zone.localize(model.end_date).astimezone(
            utc_time_zone)

    def after_model_change(self, form, model, is_created):
        # remove event permission
        models.Need.objects.filter(
            action="access_event", items=model,
            deployment=model.deployment).delete()

        # create event permission
        roles = models.Role.objects(pk__in=form.roles.data, name__ne='admin')
        models.Need.objects.create(
            action="access_event", items=[model], entities=roles,
            deployment=model.deployment)

    def scaffold_form(self):
        form_class = super(EventAdminView, self).scaffold_form()
        form_class.roles = SelectMultipleField(
            _('Roles with access'),
            choices=forms._make_choices(
                models.Role.objects(name__ne='admin').scalar('pk', 'name')),
            widget=form.Select2Widget(multiple=True))

        return form_class


class UserAdminView(BaseAdminView):
    '''Thanks to mrjoes and this Flask-Admin issue:
    https://github.com/mrjoes/flask-admin/issues/173
    '''
    column_list = ('email', 'roles', 'active')
    column_searchable_list = ('email',)
    form_excluded_columns = ('password', 'confirmed_at', 'login_count',
                             'last_login_ip', 'last_login_at',
                             'current_login_at', 'deployment',
                             'current_login_ip')
    form_rules = [
        rules.FieldSet(('email', 'password2', 'active', 'roles'))
    ]

    def get_query(self):
        user = current_user._get_current_object()
        return models.User.objects(deployment=user.deployment)

    def on_model_change(self, form, model, is_created):
        if form.password2.data:
            model.password = encrypt_password(form.password2.data)
        if is_created:
            model.deployment = current_user.deployment

    def scaffold_form(self):
        form_class = super(UserAdminView, self).scaffold_form()
        form_class.password2 = PasswordField(_('New password'))
        return form_class

    @action('disable', _('Disable'), _('Are you sure you want to disable selected users?'))
    def action_disable(self, ids):
        models.User.objects(pk__in=ids).update(set__active=False)
        flash(string_type(_('User(s) successfully disabled.')))

    @action('enable', _('Enable'), _('Are you sure you want to enable selected users?'))
    def action_enable(self, ids):
        models.User.objects(pk__in=ids).update(set__active=True)
        flash(string_type(_('User(s) successfully enabled.')))


class RoleAdminView(BaseAdminView):
    can_delete = False
    column_list = ('name',)

    def get_one(self, pk):
        role = super(RoleAdminView, self).get_one(pk)
        role.permissions = [
            unicode(i) for i in models.Need.objects(
                entities=role).scalar('pk')]
        return role

    def after_model_change(self, form, model, is_created):
        # remove model from all permissions that weren't granted
        for need in models.Need.objects(
                pk__nin=form.permissions.data):
            need.update(pull__entities=model)

        # add only the explicitly defined permissions
        for pk in form.permissions.data:
            models.Need.objects.get(pk=pk).update(add_to_set__entities=model)

    def scaffold_form(self):
        form_class = super(RoleAdminView, self).scaffold_form()
        form_class.permissions = SelectMultipleField(
            _('Permissions'),
            choices=forms._make_choices(
                models.Need.objects().scalar('pk', 'action')),
            widget=form.Select2Widget(multiple=True))

        return form_class


admin.add_view(DeploymentAdminView(models.Deployment))
admin.add_view(EventAdminView(models.Event))
admin.add_view(UserAdminView(models.User))
admin.add_view(RoleAdminView(models.Role))
