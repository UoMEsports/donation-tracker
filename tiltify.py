# Tiltify functionality for loading donation info using their API.

import datetime
import logging

import requests
from django.conf import settings
from django.core.exceptions import ValidationError

from tracker.models import Donor, Donation

TILTIFY_HOST = 'https://tiltify.com'
USER_URL = TILTIFY_HOST + '/api/v3/user'
CAMPAIGN_URL = TILTIFY_HOST + '/api/v3/users/{}/campaigns/{}'
DONATIONS_URL = TILTIFY_HOST + '/api/v3/campaigns/{}/donations?count=100'

logger = logging.getLogger(__name__)


def _get_tiltify_data(url):
    headers = {
        'Authorization': 'Bearer {}'.format(settings.TILTIFY_ACCESS_TOKEN),
    }
    r = requests.get(url, headers=headers)

    if r.status_code != 200:
        logger.error("Error getting URL {0!r} - status {1}".format(url, r.status_code))
        raise requests.exceptions.HTTPError(r.status_code)

    data = r.json()

    # In V3 API, there should be a "meta" object with a status.
    meta_status = data.get('meta', {}).get('status')
    if meta_status != 200:
        logger.error("Error getting URL {0!r} - meta status {1}".format(url, meta_status))
        raise requests.exceptions.HTTPError(meta_status)

    # Return "data" object from V3 response.
    return data['data'], data.get('links', {})


def get_user_data():
    """

    Returns:
        dict: User data object.

    """
    return _get_tiltify_data(USER_URL)[0]


def get_campaign_data(api_key, user=None):
    """Get campaign data for the given Tiltify API key.

    :param api_key: API key of the campaign to retrieve.
    :type api_key: str
    :param user: User data object, if we already got it.
    :type user: dict
    :return: Donation data object.
    :rtype: dict
    """
    if not user:
        user = get_user_data()
    return _get_tiltify_data(CAMPAIGN_URL.format(user['slug'], api_key))[0]


def get_donation_data(campaign):
    """Get donations for the given Tiltify API key.

    :param campaign: Campaign data object.
    :type campaign: dict
    :return: List of donations.
    :rtype: list[dict]
    """
    donations = []
    url = DONATIONS_URL.format(campaign['id'])

    # Loop through paging until we have no prev since donations come in descending timestamp order.
    while url:
        data, links = _get_tiltify_data(url)
        donations += data
        url = links.get('prev')
        if url:
            url = TILTIFY_HOST + url

    return donations


def sync_event_donations(event):
    """Sync donations from a Tiltify campaign with an event in our system.

    :param event: Event record to merge.
    :type event: tracker.models.Event
    :return: Number of donations updated.
    :rtype: int
    """
    if not event.tiltify_api_key:
        raise ValidationError("API key not set")

    # Get campaign data to update start date for event.
    user = get_user_data()
    t_campaign = get_campaign_data(event.tiltify_api_key, user)

    start = datetime.datetime.fromtimestamp(t_campaign['startsAt'] / 1000, datetime.timezone.utc)
    if start:
        event.datetime = start

    event.save()

    # Get donations from Tiltify API.
    t_donations = get_donation_data(t_campaign)
    num_donations = 0

    for t_donation in t_donations:
        # Get donor based on alias.
        donor = None
        if t_donation['name'] and t_donation['name'] != 'Anonymous':
            try:
                donor = Donor.objects.get(alias__iexact=t_donation['name'])
            except Donor.DoesNotExist:
                donor = Donor(email=t_donation['name'], alias=t_donation['name'])
                donor.save()

        # Get donation based on payment reference.
        try:
            donation = Donation.objects.select_for_update().get(domain='TILTIFY', domainId=t_donation['id'])
        except Donation.DoesNotExist:
            donation = Donation(event=event, domain='TILTIFY', domainId=t_donation['id'], readstate='PENDING',
                                commentstate='PENDING', donor=donor)

        # Make sure this donation wasn't already imported for a different event.
        if donation.event != event:
            raise ValidationError("Donation {!r} already exists for a different event".format(donation.domainId))

        donation.transactionstate = 'COMPLETED'
        donation.amount = t_donation['amount']
        donation.currency = event.paypalcurrency
        donation.timereceived = datetime.datetime.fromtimestamp(t_donation['completedAt'] / 1000, datetime.timezone.utc)
        donation.testdonation = event.usepaypalsandbox

        # Comment might be null from Tiltify, but can't be null on our end.
        if t_donation['comment']:
            donation.comment = t_donation['comment']
        else:
            donation.comment = ''

        donation.save()
        num_donations += 1

    return num_donations
