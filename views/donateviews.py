import collections
import datetime
import json
import random
import traceback
from decimal import Decimal

import post_office.mail
import pytz
from django.conf import settings
from django.core import serializers
from django.db import transaction
from django.db.models import Sum
from django.http import HttpResponse, Http404
from django.urls import reverse
from django.views.decorators.cache import never_cache, cache_page
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView, FormView

import tracker.eventutil as eventutil
import tracker.filters as filters
import tracker.forms as forms
import tracker.models as models
import tracker.paypalutil as paypalutil
import tracker.viewutil as viewutil
from . import common as views_common

__all__ = [
  'paypal_cancel',
  'paypal_return',
  'donate',
  'ipn',
  ]

@csrf_exempt
def paypal_cancel(request):
  return views_common.tracker_response(request, "tracker/paypal_cancel.html")

@csrf_exempt
def paypal_return(request):
  return views_common.tracker_response(request, "tracker/paypal_return.html")

@csrf_exempt
@cache_page(300)
def donate_orig(request, event):
  event = viewutil.get_event(event)
  if event.locked:
    raise Http404
  bidsFormPrefix = "bidsform"
  prizeFormPrefix = "prizeForm"
  if request.method == 'POST':
    commentform = forms.DonationEntryForm(event=event,data=request.POST)
    if commentform.is_valid():
      prizesform = forms.PrizeTicketFormSet(amount=commentform.cleaned_data['amount'], data=request.POST, prefix=prizeFormPrefix)
      bidsform = forms.DonationBidFormSet(amount=commentform.cleaned_data['amount'], data=request.POST, prefix=bidsFormPrefix)
      if bidsform.is_valid() and prizesform.is_valid():
        with transaction.atomic():
          donation = models.Donation(amount=commentform.cleaned_data['amount'], timereceived=pytz.utc.localize(datetime.datetime.utcnow()), domain='PAYPAL', domainId=str(random.getrandbits(128)), event=event, testdonation=event.usepaypalsandbox)
          if commentform.cleaned_data['comment']:
            donation.comment = commentform.cleaned_data['comment']
            donation.commentstate = "PENDING"
          donation.requestedvisibility = commentform.cleaned_data['requestedvisibility']
          donation.requestedalias = commentform.cleaned_data['requestedalias']
          donation.requestedemail = commentform.cleaned_data['requestedemail']
          donation.requestedsolicitemail = commentform.cleaned_data['requestedsolicitemail']
          donation.currency = event.paypalcurrency
          donation.save()
          for bidform in bidsform:
            if 'bid' in bidform.cleaned_data and bidform.cleaned_data['bid']:
              bid = bidform.cleaned_data['bid']
              if bid.allowuseroptions:
                # unfortunately, you can't use get_or_create when using a non-atomic transaction
                # this does technically introduce a race condition, I'm just going to hope that two people don't
                # suggest the same option at the exact same time
                # also, I want to do case-insensitive comparison on the name
                try:
                  bid = models.Bid.objects.get(event=bid.event, speedrun=bid.speedrun, name__iexact=bidform.cleaned_data['customoptionname'], parent=bid)
                except models.Bid.DoesNotExist:
                  bid = models.Bid.objects.create(event=bid.event, speedrun=bid.speedrun, name=bidform.cleaned_data['customoptionname'], parent=bid, state='PENDING', istarget=True)
              donation.bids.add(models.DonationBid(bid=bid, amount=Decimal(bidform.cleaned_data['amount'])), bulk=False)
          for prizeform in prizesform:
            if 'prize' in prizeform.cleaned_data and prizeform.cleaned_data['prize']:
              prize = prizeform.cleaned_data['prize']
              donation.tickets.add(models.PrizeTicket(prize=prize, amount=Decimal(prizeform.cleaned_data['amount'])))
          donation.full_clean()
          donation.save()

        paypal_dict = {
          "amount": str(donation.amount),
          "cmd": "_donations",
          "business": donation.event.paypalemail,
          "item_name": donation.event.receivername,
          "notify_url": request.build_absolute_uri(reverse('tracker:ipn')),
          "return_url": request.build_absolute_uri(reverse('tracker:paypal_return')),
          "cancel_return": request.build_absolute_uri(reverse('tracker:paypal_cancel')),
          "custom": str(donation.id) + ":" + donation.domainId,
          "currency_code": donation.event.paypalcurrency,
          "no_shipping": 0,
        }
        # Create the form instance
        form = forms.PayPalDonationsForm(button_type="donate", sandbox=donation.event.usepaypalsandbox, initial=paypal_dict)
        context = {"event": donation.event, "form": form }
        return views_common.tracker_response(request, "tracker/paypal_redirect.html", context)
    else:
      bidsform = forms.DonationBidFormSet(amount=Decimal('0.00'), data=request.POST, prefix=bidsFormPrefix)
      prizesform = forms.PrizeTicketFormSet(amount=Decimal('0.00'), data=request.POST, prefix=prizeFormPrefix)
  else:
    commentform = forms.DonationEntryForm(event=event)
    bidsform = forms.DonationBidFormSet(amount=Decimal('0.00'), prefix=bidsFormPrefix)
    prizesform = forms.PrizeTicketFormSet(amount=Decimal('0.00'), prefix=prizeFormPrefix)

  def bid_parent_info(bid):
    if bid != None:
      return {'name': bid.name, 'description': bid.description, 'parent': bid_parent_info(bid.parent) }
    else:
      return None

  def bid_info(bid):
    result = {
      'id': bid.id,
      'name': bid.name,
      'description': bid.description,
      'label': bid.full_label(not bid.allowuseroptions),
      'count': bid.count,
      'amount': bid.total,
      'goal': Decimal(bid.goal or '0.00'),
      'parent': bid_parent_info(bid.parent)
    }
    if bid.speedrun:
      result['runname'] = bid.speedrun.name
    if bid.suggestions.exists():
      result['suggested'] = list([x.name for x in bid.suggestions.all()])
    if bid.allowuseroptions:
      result['custom'] = ['custom']
      result['label'] += ' (select and add a name next to "New Option Name")'
    return result

  bids = filters.run_model_query('bidtarget', {'state':'OPENED', 'event':event.id }, user=request.user).distinct().select_related('parent').prefetch_related('suggestions')

  allPrizes = filters.run_model_query('prize', {'feed': 'current', 'event': event.id })

  prizes = allPrizes.filter(ticketdraw=False)

  dumpArray = [bid_info(o) for o in bids]

  bidsJson = json.dumps(dumpArray, ensure_ascii=False, cls=serializers.json.DjangoJSONEncoder)

  ticketPrizes = allPrizes.filter(ticketdraw=True, auto_tickets=False)

  def prize_info(prize):
    result = {'id': prize.id, 'name': prize.name, 'description': prize.description, 'minimumbid': prize.minimumbid, 'maximumbid': prize.maximumbid, 'sumdonations': prize.sumdonations}
    return result

  dumpArray = [prize_info(o) for o in ticketPrizes.all()]
  ticketPrizesJson = json.dumps(dumpArray, ensure_ascii=False, cls=serializers.json.DjangoJSONEncoder)

  return views_common.tracker_response(request, "tracker/donate.html", {
    'event': event,
    'bidsform': bidsform,
    'prizesform': prizesform,
    'commentform': commentform,
    'hasBids': bids.count() > 0,
    'bidsJson': bidsJson,
    'hasTicketPrizes': ticketPrizes.count() > 0,
    'ticketPrizesJson': ticketPrizesJson,
    'prizes': prizes,
    'site_name': settings.SITE_NAME,
  })


class DonateViewV2(TemplateView):
  template_name = 'tracker/donate_v2.html'

  def get_context_data(self, **kwargs):
    context = super().get_context_data(**kwargs)

    # Get event
    event = viewutil.get_event(context['event'])
    if event.locked:
      raise Http404
    context['event'] = event

    # Base donation info form.
    context['commentform'] = forms.DonationEntryFormV2(event=event, data=self.request.POST or None)

    # Get donation amount if form is valid.
    if self.request.method == 'POST' and context['commentform'].is_valid():
      amount = context['commentform'].cleaned_data['amount']
    else:
      amount = Decimal('0.00')

    # Bid selection form
    bids = filters.run_model_query(
      'bid', {'state': 'OPENED', 'event': event.id}, user=self.request.user).select_related(
      'speedrun').prefetch_related('options')
    context['bidsform'] = forms.DonationBidFormV2(amount=amount, bids=bids, data=self.request.POST or None)
    context['hasBids'] = bids.count() > 0
    context['bids'] = bids

    # Group bids by run for the revamped donate page display.
    bids_by_run = collections.OrderedDict()
    for bid in bids:
      if bid.speedrun not in bids_by_run:
        bids_by_run[bid.speedrun] = []

      bid.options_list = list(bid.options.filter(state='OPENED').order_by('-total', 'name'))
      bids_by_run[bid.speedrun].append(bid)

    context['bids_by_run'] = []
    for run, bids in bids_by_run.items():
      run.bid_list = bids
      context['bids_by_run'].append(run)

    return context

  def post(self, request, *args, **kwargs):
    context = self.get_context_data(**kwargs)
    comment_form = context['commentform']
    bids_form = context['bidsform']

    if comment_form.is_valid() and bids_form.is_valid():
      with transaction.atomic():
        # Make the new donation records.
        donation = models.Donation(amount=comment_form.cleaned_data['amount'],
                                   timereceived=pytz.utc.localize(datetime.datetime.utcnow()), domain='PAYPAL',
                                   domainId=str(random.getrandbits(128)), event=context['event'],
                                   testdonation=context['event'].usepaypalsandbox)
        if comment_form.cleaned_data['comment']:
          donation.comment = comment_form.cleaned_data['comment']
          donation.commentstate = "PENDING"
        # Use "alias only" visibility if alias is provided, otherwise use current value (default anonymous).
        donation.requestedvisibility = "ALIAS" if comment_form.cleaned_data['requestedalias'] else "CURR"
        donation.requestedalias = comment_form.cleaned_data['requestedalias']
        donation.requestedemail = comment_form.cleaned_data['requestedemail']
        donation.requestedsolicitemail = "CURR"
        donation.currency = context['event'].paypalcurrency
        donation.save()

        # Make bid records for the donation.
        for bid in context['bids']:
          amt_field = 'bid_amt_{}'.format(bid.id)

          # Check if the user is adding a new option for a bid war.
          if bid.allowuseroptions:
            opt_field = 'bid_new_option_name_{}'.format(bid.id)

            if bids_form.cleaned_data.get(amt_field) and bids_form.cleaned_data.get(opt_field):
              try:
                option = models.Bid.objects.get(event=bid.event, speedrun=bid.speedrun,
                                                name__iexact=bids_form.cleaned_data[opt_field], parent=bid)
              except models.Bid.DoesNotExist:
                option = models.Bid.objects.create(event=bid.event, speedrun=bid.speedrun,
                                                   name=bids_form.cleaned_data[opt_field], parent=bid,
                                                  state='PENDING', istarget=True)
              donation.bids.add(models.DonationBid(bid=option, amount=Decimal(bids_form.cleaned_data[amt_field])),
                                bulk=False)

          # Otherwise, check if this bid is a target.
          elif bid.istarget:
            if bids_form.cleaned_data.get(amt_field):
              donation.bids.add(models.DonationBid(bid=bid, amount=Decimal(bids_form.cleaned_data[amt_field])),
                                bulk=False)

          # Check any of its children if they are targets.
          for option in bid.options.all():
            opt_amt_field = 'bid_amt_{}'.format(option.id)
            if option.istarget and bids_form.cleaned_data.get(opt_amt_field):
              donation.bids.add(models.DonationBid(bid=option, amount=Decimal(bids_form.cleaned_data[opt_amt_field])),
                                bulk=False)

        donation.full_clean()
        donation.save()

      # Do the PayPal thing.
      paypal_dict = {
        "amount": str(donation.amount),
        "cmd": "_donations",
        "business": donation.event.paypalemail,
        "item_name": donation.event.receivername,
        "notify_url": request.build_absolute_uri(reverse('tracker:ipn')),
        "return_url": request.build_absolute_uri(reverse('tracker:paypal_return')),
        "cancel_return": request.build_absolute_uri(reverse('tracker:donate', args=[context['event'].short])),
        "custom": str(donation.id) + ":" + donation.domainId,
        "currency_code": donation.event.paypalcurrency,
        "no_shipping": 0,
      }
      # Create the form instance
      form = forms.PayPalDonationsForm(button_type="donate", sandbox=donation.event.usepaypalsandbox,
                                       initial=paypal_dict)
      context = {"event": donation.event, "form": form}
      return views_common.tracker_response(request, "tracker/paypal_redirect.html", context)

    return self.render_to_response(context)


# Are we using the original view page or the revamped version?
# Define donate view based on this setting so the URLs
if settings.USE_NEW_DONATE_LAYOUT:
  donate = DonateViewV2.as_view()
else:
  donate = donate_orig


@csrf_exempt
@never_cache
def ipn(request):
  ipnObj = None

  if request.method == 'GET' or len(request.POST) == 0:
    return views_common.tracker_response(request, "tracker/badobject.html", {})

  try:
    ipnObj = paypalutil.create_ipn(request)
    ipnObj.save()

    donation = paypalutil.initialize_paypal_donation(ipnObj)
    donation.save()

    if donation.transactionstate == 'PENDING':
      reasonExplanation, ourFault = paypalutil.get_pending_reason_details(ipnObj.pending_reason)
      if donation.event.pendingdonationemailtemplate:
        formatContext = {
          'event': donation.event,
          'donation': donation,
          'donor': donation.donor,
          'pending_reason': ipnObj.pending_reason,
          'reason_info': reasonExplanation if not ourFault else '',
        }
        post_office.mail.send(recipients=[donation.donor.email], sender=donation.event.donationemailsender, template=donation.event.pendingdonationemailtemplate, context=formatContext)
      # some pending reasons can be a problem with the receiver account, we should keep track of them
      if ourFault:
        paypalutil.log_ipn(ipnObj, 'Unhandled pending error')
    elif donation.transactionstate == 'COMPLETED':
      if donation.event.donationemailtemplate != None:
        formatContext = {
          'donation': donation,
          'donor': donation.donor,
          'event': donation.event,
          'prizes': viewutil.get_donation_prize_info(donation),
        }
        post_office.mail.send(recipients=[donation.donor.email], sender=donation.event.donationemailsender, template=donation.event.donationemailtemplate, context=formatContext)
        
      eventutil.post_donation_to_postbacks(donation)
  
    elif donation.transactionstate == 'CANCELLED':
      # eventually we may want to send out e-mail for some of the possible cases
      # such as payment reversal due to double-transactions (this has happened before)
      paypalutil.log_ipn(ipnObj, 'Cancelled/reversed payment')

  except Exception as inst:
    # just to make sure we have a record of it somewhere
    print("ERROR IN IPN RESPONSE, FIX IT")
    if ipnObj:
      paypalutil.log_ipn(ipnObj, "{0} \n {1}. POST data : {2}".format(inst, traceback.format_exc(inst), request.POST))
    else:
      viewutil.tracker_log('paypal', 'IPN creation failed: {0} \n {1}. POST data : {2}'.format(inst, traceback.format_exc(inst), request.POST))
  return HttpResponse("OKAY")
