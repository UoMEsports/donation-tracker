from django import forms
from django.contrib.auth.models import User
from django.utils.translation import ugettext as _
from django.utils.safestring import mark_safe
from tracker import models
import paypal
import re
from decimal import *
import multiform;
from django.forms import formsets;
import django.core.exceptions;

import tracker.fields;
import tracker.widgets;

def positive(value):
  if value <  0: raise ValidationError('Value cannot be negative')

def nonzero(value):
  if value == 0: raise ValidationError('Value cannot be zero') 

class UsernameForm(forms.Form):
  username = forms.CharField(
    max_length=255,
    widget=forms.TextInput(attrs={'class': 'required username'}))
  def clean_username(self):
    if 'username' in self.cleaned_data:
      username = self.cleaned_data['username']
      if not re.match(r'^[a-zA-Z0-9_]+$', username):
        raise forms.ValidationError(_("Usernames can only contain letters, numbers, and the underscore"))
      if username[:10]=='openiduser':
        raise forms.ValidationError(_("Username may not start with 'openiduser'"))
      if User.objects.filter(username=username).count() > 0:
        raise forms.ValidationError(_("Username already in use"))
      return self.cleaned_data['username']

class DonationCredentialsForm(forms.Form):
  paypalemail = forms.EmailField(min_length=1, label="Paypal Email");
  amount = forms.DecimalField(decimal_places=2, min_value=Decimal('0.00'), label="Donation Amount");
  transactionid = forms.CharField(min_length=1, label="Transaction ID");

class DonationCommentForm(forms.Form):
  comment = forms.CharField(widget=forms.Textarea, required=False);
  hasbid = forms.BooleanField(label="Is this a bid suggestion?");

class DonationBidForm(forms.Form):
  bid = tracker.fields.DonationBidField(label="");
  amount = forms.DecimalField(decimal_places=2,max_digits=20,validators=[positive,nonzero], widget=forms.widgets.TextInput(attrs={'class': 'cdonationbidamount'}));
  def clean_bid(self):
    try:
      bid = self.cleaned_data['bid'];
      if bid[0] == 'choice':
        bid = models.ChoiceOption.objects.get(id=bid[1]);
        if bid.choice.state == 'CLOSED':
          raise forms.ValidationError("This bid not open for new donations anymore.");
      elif bid[0] == 'challenge':
        bid = models.Challenge.objects.get(id=int(bid[1]));
        if bid.state == 'CLOSED':
          raise forms.ValidationError("This bid not open for new donations anymore.");
      else:
        raise forms.ValidationError("Invalid bid type.");
    except Exception as e:
      raise forms.ValidationError("Bid does not exist.");
    return bid;
      
class DonationBidFormSetBase(forms.formsets.BaseFormSet):
  max_bids = 10;

  def __init__(self, donation=None, *args, **kwargs):
    self.donation = donation;
    print(str(donation));
    super(DonationBidFormSetBase, self).__init__(*args, **kwargs);

  def collect_bids(self):
    bids = [];
    for bid in self.donation.challengebid_set.all():
      bids.append(bid);
    for bid in self.donation.choicebid_set.all():
      bids.append(bid);
    allocatedSoFar = Decimal('0.00');
    for bid in bids:
      allocatedSoFar += bid.amount;
    return bids, allocatedSoFar;
  
  def clean(self):
    if any(self.errors):
      # Don't bother validating the formset unless each form is valid on its own
      return;
    bids, allocatedSoFar = self.collect_bids();
    if len(self.forms) > DonationBidFormSetBase.max_bids - len(bids):
      form.errors['__all__'] = form.error_class(["Error, cannot submit more than " + max_bids + " bids."]);
      raise forms.ValidationError("Error, cannot submit more than " + max_bids + " bids.");
    sumAmount = Decimal('0.00');
    for form in self.forms:
      if 'amount' in form.cleaned_data:
        sumAmount += form.cleaned_data['amount'];
      if sumAmount > self.donation.amount - allocatedSoFar:
        form.errors['__all__'] = form.error_class(["Error, total bid amount cannot exceed donation amount."]);
        raise forms.ValidationError("Error, total bid amount cannot exceed donation amount.");
  
DonationBidFormSet = formsets.formset_factory(DonationBidForm, formset=DonationBidFormSetBase);

"""
class DonationPostbackForm(multiform.MultiForm):
  base_forms = {
    'comment': DonationCommentForm,
    'bids': DonationBidsForm,
  };
"""

class DonorSearchForm(forms.Form):
  q = forms.CharField(required=False, initial=None, max_length=255, label='Search');

class DonationSearchForm(forms.Form):
  feed = forms.ChoiceField(required=False, initial='recent', choices=(('all', 'All'), ('recent', 'Recent')), label='Filter')
  q = forms.CharField(required=False, initial=None, max_length=255, label='Search');
  
class BidSearchForm(forms.Form):
  feed = forms.ChoiceField(required=False, initial='current', choices=(('all', 'All'), ('current', 'Current'), ('future', 'Future'), ('open','Open'), ('closed', 'Closed')), label='Type');
  q = forms.CharField(required=False, initial=None, max_length=255, label='Search');

class RunSearchForm(forms.Form):
  feed = forms.ChoiceField(required=False, initial='current', choices=(('all','All'), ('current','Current'), ('future', 'Future')), label='Type')
  q = forms.CharField(required=False, initial=None, max_length=255, label='Search');

class PrizeSearchForm(forms.Form):
  feed = forms.ChoiceField(required=False, initial='upcomming', choices=(('all', 'All'), ('unwon', 'Not Drawn'), ('won', 'Drawn'), ('current', 'Current'), ('upcomming', 'Upcomming')), label='Type')
  q = forms.CharField(required=False, initial=None, max_length=255, label='Search');

