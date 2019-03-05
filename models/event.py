import datetime
import decimal
import re

import post_office.models
import pytz
from django.contrib.auth.models import User
from django.core import validators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.utils import OperationalError
from django.utils.html import format_html
from timezone_field import TimeZoneField

import tracker.util as util
from ..validators import *

__all__ = [
    'Event',
    'PostbackURL',
    'SpeedRun',
    'Runner',
    'Submission',
]

_timezoneChoices = list([(x,x) for x in pytz.common_timezones])
_currencyChoices = (('GBP', 'Pound Sterling'), ('USD', 'US Dollars'), ('CAD', 'Canadian Dollars'))


class TimestampValidator(validators.RegexValidator):
    regex = r'(?:(?:(\d+):)?(?:(\d+):))?(\d+)(?:\.(\d{1,3}))?$'

    def __call__(self, value):
        super(TimestampValidator, self).__call__(value)
        h, m, s, ms = re.match(self.regex, str(value)).groups()
        if h is not None and int(m) >= 60:
            raise ValidationError('Minutes cannot be 60 or higher if the hour part is specified')
        if m is not None and int(s) >= 60:
            raise ValidationError('Seconds cannot be 60 or higher if the minute part is specified')


class TimestampField(models.Field):
    default_validators = [TimestampValidator()]
    match_string = re.compile(r'(?:(?:(\d+):)?(?:(\d+):))?(\d+)(?:\.(\d+))?')

    def __init__(self, always_show_h=False, always_show_m=False, always_show_ms=False, *args, **kwargs):
        super(TimestampField, self).__init__(*args, **kwargs)
        self.always_show_h = always_show_h
        self.always_show_m = always_show_m
        self.always_show_ms = always_show_ms

    def from_db_value(self, value, expression, connection, context):
        return self.to_python(value)

    def to_python(self, value):
        if isinstance(value, str):
            try:
                value = TimestampField.time_string_to_int(value)
            except ValueError:
                return value
        if not value:
            return '0'
        h, m, s, ms = value / 3600000, value / 60000 % 60, value / 1000 % 60, value % 1000
        if h or self.always_show_h:
            if ms or self.always_show_ms:
                return '%d:%02d:%02d.%03d' % (h, m, s, ms)
            else:
                return '%d:%02d:%02d' % (h, m, s)
        elif m or self.always_show_m:
            if ms or self.always_show_ms:
                return '%d:%02d.%03d' % (m, s, ms)
            else:
                return '%d:%02d' % (m, s)
        else:
            if ms or self.always_show_ms:
                return '%d.%03d' % (s, ms)
            else:
                return '%d' % s

    @staticmethod
    def time_string_to_int(value):
        try:
            if str(int(value)) == value:
                return int(value) * 1000
        except ValueError:
            pass
        if not isinstance(value, str):
            return value
        if not value:
            return 0
        match = TimestampField.match_string.match(value)
        if not match:
            raise ValueError('Not a valid timestamp: ' + value)
        h, m, s, ms = match.groups()
        s = int(s)
        m = int(m or s / 60)
        s %= 60
        h = int(h or m / 60)
        m %= 60
        ms = int(ms or 0)
        return h * 3600000 + m * 60000 + s * 1000 + ms

    def get_prep_value(self, value):
        return TimestampField.time_string_to_int(value)

    def get_internal_type(self):
        return 'IntegerField'

    def validate(self, value, model_instance):
        super(TimestampField, self).validate(value, model_instance)
        try:
            TimestampField.time_string_to_int(value)
        except ValueError:
            raise ValidationError('Not a valid timestamp')


class EventManager(models.Manager):
    def get_by_natural_key(self, short):
        return self.get(short=short)


class Event(models.Model):
    objects = EventManager()
    short = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    receivername = models.CharField(max_length=128, blank=True, null=False, verbose_name='Receiver Name')
    targetamount = models.DecimalField(decimal_places=2, max_digits=20, validators=[positive, nonzero],
                                       verbose_name='Target Amount')
    minimumdonation = models.DecimalField(decimal_places=2, max_digits=20, validators=[positive, nonzero],
                                          verbose_name='Minimum Donation',
                                          help_text='Enforces a minimum donation amount on the donate page.',
                                          default=decimal.Decimal('1.00'))
    usepaypalsandbox = models.BooleanField(default=False, verbose_name='Use Paypal Sandbox')
    paypalemail = models.EmailField(max_length=128, null=False, blank=False, verbose_name='Receiver Paypal')
    paypalcurrency = models.CharField(max_length=8, null=False, blank=False, default=_currencyChoices[0][0],
                                      choices=_currencyChoices, verbose_name='Currency')
    donationemailtemplate = models.ForeignKey(post_office.models.EmailTemplate, verbose_name='Donation Email Template',
                                              default=None, null=True, blank=True, on_delete=models.PROTECT,
                                              related_name='event_donation_templates')
    pendingdonationemailtemplate = models.ForeignKey(post_office.models.EmailTemplate,
                                                     verbose_name='Pending Donation Email Template', default=None,
                                                     null=True, blank=True, on_delete=models.PROTECT,
                                                     related_name='event_pending_donation_templates')
    donationemailsender = models.EmailField(max_length=128, null=True, blank=True, verbose_name='Donation Email Sender')
    scheduleid = models.CharField(max_length=128, unique=True, null=True, blank=True,
                                  verbose_name='Schedule ID (LEGACY)', editable=False)
    datetime = models.DateTimeField()
    timezone = TimeZoneField(default='US/Eastern')
    locked = models.BooleanField(default=False,
                                 help_text='Requires special permission to edit this event or anything associated with it')
    # Fields related to prize management
    prizecoordinator = models.ForeignKey(User, default=None, null=True, blank=True, verbose_name='Prize Coordinator',
                                         help_text='The person responsible for managing prize acceptance/distribution',
                                         on_delete=models.PROTECT)
    allowed_prize_countries = models.ManyToManyField('Country', blank=True, verbose_name="Allowed Prize Countries",
                                                     help_text="List of countries whose residents are allowed to receive prizes (leave blank to allow all countries)")
    disallowed_prize_regions = models.ManyToManyField('CountryRegion', blank=True, verbose_name='Disallowed Regions',
                                                      help_text='A blacklist of regions within allowed countries that are not allowed for drawings (e.g. Quebec in Canada)')
    prize_accept_deadline_delta = models.IntegerField(default=14, null=False, blank=False,
                                                      verbose_name='Prize Accept Deadline Delta',
                                                      help_text='The number of days a winner will be given to accept a prize before it is re-rolled.',
                                                      validators=[positive, nonzero])
    prizecontributoremailtemplate = models.ForeignKey(post_office.models.EmailTemplate, default=None, null=True,
                                                      blank=True,
                                                      verbose_name='Prize Contributor Accept/Deny Email Template',
                                                      help_text="Email template to use when responding to prize contributor's submission requests",
                                                      related_name='event_prizecontributortemplates',
                                                      on_delete=models.PROTECT)
    prizewinneremailtemplate = models.ForeignKey(post_office.models.EmailTemplate, default=None, null=True, blank=True,
                                                 verbose_name='Prize Winner Email Template',
                                                 help_text="Email template to use when someone wins a prize.",
                                                 related_name='event_prizewinnertemplates', on_delete=models.PROTECT)
    prizewinneracceptemailtemplate = models.ForeignKey(post_office.models.EmailTemplate, default=None, null=True,
                                                       blank=True, verbose_name='Prize Accepted Email Template',
                                                       help_text="Email template to use when someone accepts a prize (and thus it needs to be shipped).",
                                                       related_name='event_prizewinneraccepttemplates',
                                                       on_delete=models.PROTECT)
    prizeshippedemailtemplate = models.ForeignKey(post_office.models.EmailTemplate, default=None, null=True, blank=True,
                                                  verbose_name='Prize Shipped Email Template',
                                                  help_text="Email template to use when the aprize has been shipped to its recipient).",
                                                  related_name='event_prizeshippedtemplates', on_delete=models.PROTECT)
    # Fields for Horaro schedule import
    horaro_id = models.CharField(max_length=100, verbose_name='Event ID', blank=True, default='',
                                 help_text='ID or slug for Horaro event')
    horaro_game_col = models.IntegerField(verbose_name='Game Column', blank=True, null=True,
                                          help_text='Column index for game info (start at 0)')
    horaro_category_col = models.IntegerField(verbose_name='Category Column', blank=True, null=True,
                                              help_text='Column index for category info (start at 0)')
    horaro_runners_col = models.IntegerField(verbose_name='Runners Column', blank=True, null=True,
                                             help_text='Column index for runner info (start at 0)')
    horaro_commentators_col = models.IntegerField(verbose_name='Commentators Column', blank=True, null=True,
                                                  help_text='Column index for commentator info (start at 0)')

    # Fields for Tiltify donation import
    tiltify_enable_sync = models.BooleanField(default=False, verbose_name='Enable Tiltify Sync',
                                              help_text='Sync donations for this event via the Tiltify API')
    tiltify_api_key = models.CharField(max_length=100, verbose_name='Tiltify Campaign API Key', blank=True, default='')

    # Fields for Twitch chat announcements
    twitch_channel = models.CharField(max_length=100, verbose_name='Channel Name', blank=True, default='',
                                      help_text='Announcements will be made to this channel')
    twitch_login = models.CharField(max_length=100, verbose_name='Username', blank=True, default='',
                                    help_text='Username to use for chat announcements')
    twitch_oauth = models.CharField(max_length=200, verbose_name='OAuth Password', blank=True, default='',
                                    help_text='Get one here: http://www.twitchapps.com/tmi')

    def __str__(self):
        return self.name

    def natural_key(self):
        return (self.short,)

    def save(self, *args, **kwargs):
        if self.datetime is not None:
            if self.datetime.tzinfo is None or self.datetime.tzinfo.utcoffset(self.datetime) is None:
                self.datetime = self.timezone.localize(self.datetime)
        super(Event, self).save(*args, **kwargs)

    def clean(self):
        errors = {}

        if self.id and self.id < 1:
            raise ValidationError('Event ID must be positive and non-zero')
        if not re.match('^\w+$', self.short):
            errors['short'] = 'Event short name must be a url-safe string'
        if not self.scheduleid:
            self.scheduleid = None
        if self.donationemailtemplate != None or self.pendingdonationemailtemplate != None:
            if not self.donationemailsender:
                errors['donationemailsender'] = 'Must specify a donation email sender if automailing is used'

        # If Tiltify sync is enabled, the API key must be populated.
        if self.tiltify_enable_sync and not self.tiltify_api_key:
            errors['tiltify_api_key'] = 'Must be populated if Tiltify sync is enabled'

        # If any Twitch chat fields are filled in, they all must be.
        if self.twitch_channel or self.twitch_login or self.twitch_oauth:
            if not self.twitch_channel:
                errors['twitch_channel'] = 'Must be filled if enabling Twitch chat announcements'
            if not self.twitch_login:
                errors['twitch_login'] = 'Must be filled if enabling Twitch chat announcements'
            if not self.twitch_oauth:
                errors['twitch_oauth'] = 'Must be filled if enabling Twitch chat announcements'

        # Don't put the "oauth:" starting part on the token.  IRC code will add this automatically.
        if self.twitch_oauth.startswith("oauth:"):
            self.twitch_oauth = self.twitch_oauth[6:]

        if errors:
            raise ValidationError(errors)

    @property
    def date(self):
        return self.datetime.date()

    # Extra field for displaying Horaro columns on admin UI.
    def admin_horaro_check_cols(self):
        return format_html('<span id="horaro_cols"></span>')

    admin_horaro_check_cols.allow_tags = True
    admin_horaro_check_cols.short_description = "Schedule Columns"

    class Meta:
        app_label = 'tracker'
        get_latest_by = 'datetime'
        permissions = (
            ('can_edit_locked_events', 'Can edit locked events'),
        )
        ordering = ('datetime', 'name')


def LatestEvent():
    if Event.objects.exists():
        try:
            return Event.objects.latest()
        except (Event.DoesNotExist, OperationalError):
            return None
    return None


class PostbackURL(models.Model):
    event = models.ForeignKey('Event', on_delete=models.PROTECT, verbose_name='Event', null=False, blank=False,
                              related_name='postbacks')
    url = models.URLField(blank=False, null=False, verbose_name='URL')

    class Meta:
        app_label = 'tracker'


class SpeedRunManager(models.Manager):
    def get_by_natural_key(self, name, event):
        return self.get(name=name, event=Event.objects.get_by_natural_key(*event))

    def get_or_create_by_natural_key(self, name, event):
        return self.get_or_create(name=name, event=Event.objects.get_by_natural_key(*event))


def runners_exists(runners):
    for r in runners.split(','):
        try:
            Runner.objects.get_by_natural_key(r.strip())
        except Runner.DoesNotExist:
            raise ValidationError('Runner not found: "%s"' % r.strip())


class SpeedRun(models.Model):
    objects = SpeedRunManager()
    event = models.ForeignKey('Event', on_delete=models.PROTECT, default=LatestEvent)
    name = models.CharField(max_length=64)
    display_name = models.TextField(max_length=256, blank=True, verbose_name='Display Name',
                                    help_text='How to display this game on the stream.')
    # This field is now deprecated, we should eventually set up a way to migrate the old set-up to use the donor links
    deprecated_runners = models.CharField(max_length=1024, blank=True, verbose_name='*DEPRECATED* Runners',
                                          editable=False, validators=[runners_exists])
    console = models.CharField(max_length=32, blank=True)
    commentators = models.CharField(max_length=1024, blank=True)
    description = models.TextField(max_length=1024, blank=True)
    starttime = models.DateTimeField(verbose_name='Start Time', editable=False, null=True)
    endtime = models.DateTimeField(verbose_name='End Time', editable=False, null=True)
    # can be temporarily null when moving runs around, or null when they haven't been slotted in yet
    order = models.IntegerField(blank=True, null=True,
                                help_text='Please note that using the schedule editor is much easier',
                                validators=[positive])
    run_time = TimestampField(always_show_h=True)
    setup_time = TimestampField(always_show_h=True)
    runners = models.ManyToManyField('Runner')
    coop = models.BooleanField(default=False,
                               help_text='Cooperative runs should be marked with this for layout purposes')
    category = models.CharField(max_length=64, blank=True, null=True, help_text='The type of run being performed')
    release_year = models.IntegerField(blank=True, null=True, verbose_name='Release Year',
                                       help_text='The year the game was released')
    giantbomb_id = models.IntegerField(blank=True, null=True, verbose_name='GiantBomb Database ID',
                                       help_text='Identifies the game in the GiantBomb database, to allow auto-population of game data.')
    tech_notes = models.TextField(blank=True, help_text='Notes for the tech crew')

    class Meta:
        app_label = 'tracker'
        verbose_name = 'Speed Run'
        unique_together = (('name', 'category', 'event'), ('event', 'order'))
        ordering = ['event__datetime', 'order', 'name']
        permissions = (
            ('can_view_tech_notes', 'Can view tech notes'),
        )

    def natural_key(self):
        return (self.name, self.event.natural_key())

    def clean(self):
        if not self.name:
            raise ValidationError('Name cannot be blank')
        if not self.display_name:
            self.display_name = self.name
        if not self.order:
            self.order = None

    def save(self, fix_time=True, fix_runners=True, *args, **kwargs):
        i = TimestampField.time_string_to_int
        can_fix_time = self.order is not None and (i(self.run_time) != 0 or i(self.setup_time) != 0)

        # fix our own time
        if fix_time and can_fix_time:
            prev = SpeedRun.objects.filter(event=self.event, order__lt=self.order).last()
            if prev:
                self.starttime = prev.starttime + datetime.timedelta(milliseconds=i(prev.run_time) + i(prev.setup_time))
            else:
                self.starttime = self.event.datetime
            self.endtime = self.starttime + datetime.timedelta(milliseconds=i(self.run_time) + i(self.setup_time))

        if fix_runners and self.id:
            if not self.runners.exists():
                try:
                    self.runners.add(*[Runner.objects.get_by_natural_key(r) for r in
                                       util.natural_list_parse(self.deprecated_runners, symbol_only=True)])
                except Runner.DoesNotExist:
                    pass
            if self.runners.exists():
                self.deprecated_runners = ', '.join(str(r) for r in self.runners.all())

        super(SpeedRun, self).save(*args, **kwargs)

        # fix up all the others if requested
        if fix_time:
            if can_fix_time:
                next = SpeedRun.objects.filter(event=self.event, order__gt=self.order).first()
                starttime = self.starttime + datetime.timedelta(milliseconds=i(self.run_time) + i(self.setup_time))
                if next and next.starttime != starttime:
                    return [self] + next.save(*args, **kwargs)
            elif self.starttime:
                prev = SpeedRun.objects.filter(event=self.event, starttime__lte=self.starttime).exclude(
                    order=None).last()
                if prev:
                    self.starttime = prev.starttime + datetime.timedelta(
                        milliseconds=i(prev.run_time) + i(prev.setup_time))
                else:
                    self.starttime = self.event.timezone.localize(
                        datetime.datetime.combine(self.event.date, datetime.time(12)))
                next = SpeedRun.objects.filter(event=self.event, starttime__gte=self.starttime).exclude(
                    order=None).first()
                if next and next.starttime != self.starttime:
                    return [self] + next.save(*args, **kwargs)
        return [self]

    def name_with_category(self):
        categoryString = ' ' + self.category if self.category else ''
        return '{0}{1}'.format(self.name, categoryString)

    def __str__(self):
        return '{0} ({1})'.format(self.name_with_category(), self.event)


class Runner(models.Model):
    class _Manager(models.Manager):
        def get_by_natural_key(self, name):
            return self.get(name__iexact=name)

        def get_or_create_by_natural_key(self, name):
            return self.get_or_create(name=name)

    class Meta:
        app_label = 'tracker'
        ordering = ('name',)

    objects = _Manager()
    name = models.CharField(max_length=64, unique=True)
    stream = models.URLField(max_length=128, blank=True)
    twitter = models.SlugField(max_length=15, blank=True)
    youtube = models.SlugField(max_length=20, blank=True)
    donor = models.OneToOneField('tracker.Donor', blank=True, null=True, on_delete=models.PROTECT)

    def natural_key(self):
        return (self.name,)

    def __str__(self):
        return self.name


class Submission(models.Model):
    class Meta:
        app_label = 'tracker'
        ordering = ('run__event__date', 'run__order', 'run__name', 'game_name', 'category')

    external_id = models.IntegerField(primary_key=True)
    run = models.ForeignKey('SpeedRun', on_delete=models.PROTECT)
    runner = models.ForeignKey('Runner', on_delete=models.PROTECT)
    game_name = models.CharField(max_length=64)
    category = models.CharField(max_length=64)
    console = models.CharField(max_length=32)
    estimate = TimestampField(always_show_h=True)

    def __str__(self):
        return '%s (%s) by %s' % (self.game_name, self.category, self.runner)

    def save(self, *args, **kwargs):
        super(Submission, self).save(*args, **kwargs)
        ret = [self]
        save_run = False
        if not self.run.category:
            self.run.category = self.category
            save_run = True
        if not self.run.description:
            self.run.description = self.category
            save_run = True
        if not self.run.console:
            self.run.console = self.console
            save_run = True
        if not self.run.run_time:
            self.run.run_time = self.estimate
            save_run = True
        if save_run:
            self.run.save()
            ret.append(self.run)
        return ret
