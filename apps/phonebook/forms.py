import re
import tempfile

from django import forms
from django.conf import settings
from django.utils import translation

import happyforms
import Image
from easy_thumbnails import processors
from product_details import product_details
from tower import ugettext as _, ugettext_lazy as _lazy

from groups.models import Group
from locations.models import Address, Country, PostalCode
from phonebook.models import Invite
from users.models import User


PAGINATION_LIMIT = 20

REGEX_NUMERIC = re.compile('\d+', re.IGNORECASE)


class SearchForm(happyforms.Form):
    q = forms.CharField(widget=forms.HiddenInput, required=True)
    limit = forms.CharField(widget=forms.HiddenInput, required=False)
    nonvouched_only = forms.BooleanField(required=False)

    def clean_limit(self):
        """Validate that this limit is numeric and greater than 1"""
        limit = self.cleaned_data['limit']

        if not limit:
            limit = PAGINATION_LIMIT
        elif not REGEX_NUMERIC.match(str(limit)) or int(limit) < 1:
            limit = PAGINATION_LIMIT

        return limit


class ProfileForm(happyforms.Form):
    first_name = forms.CharField(label=_lazy(u'First Name'), required=False)
    last_name = forms.CharField(label=_lazy(u'Last Name'), required=True)
    biography = forms.CharField(label=_lazy(u'Bio'),
                                widget=forms.Textarea(),
                                required=False)
    photo = forms.ImageField(label=_lazy(u'Profile Photo'), required=False)
    photo_delete = forms.BooleanField(label=_lazy(u'Remove Profile Photo'),
                                      required=False)

    # Remote System Ids
    # Tightly coupled with larper.UserSession.form_to_service_ids_attrs
    irc_nickname = forms.CharField(label=_lazy(u'IRC Nickname'),
                                   required=False)
    irc_nickname_unique_id = forms.CharField(widget=forms.HiddenInput,
                                             required=False)

    groups = forms.CharField(label=_lazy(u'Groups'), required=False)
    website = forms.URLField(label=_lazy(u'Website'), required=False)

    #: L10n: Street address; not entire address
    street = forms.CharField(label=_lazy(u'Address'), required=False)
    city = forms.CharField(label=_lazy(u'City'), required=False)
    # TODO: Add validation of states/provinces/etc. for known/large countries.
    province = forms.CharField(label=_lazy(u'Province/State'), required=False)
    postal_code = forms.CharField(label=_lazy(u'Postal/Zip Code'),
                                  required=False)

    def __init__(self, *args, **kwargs):
        """Add a locale-aware list of countries to the form."""
        locale = kwargs.get('locale', 'en-US')
        if kwargs.get('locale'):
            del kwargs['locale']

        super(ProfileForm, self).__init__(*args, **kwargs)

        self.fields['country'] = forms.ChoiceField(label=_lazy(u'Country'),
                required=False, choices=([['', '--']] +
                                         Country.localized_list(locale)))

    def clean_photo(self):
        """Let's make sure things are right.

        Cribbed from zamboni.  Thanks Dave Dash!

        TODO: this needs to go into celery

        - File IT bug for celery
        - Ensure files aren't InMemory files
        - See zamboni.apps.users.forms
        """
        photo = self.cleaned_data['photo']

        if not photo:
            return

        if photo.content_type not in ('image/png', 'image/jpeg'):
            raise forms.ValidationError(
                    _('Images must be either PNG or JPG.'))

        if photo.size > settings.MAX_PHOTO_UPLOAD_SIZE:
            raise forms.ValidationError(
                    _('Please use images smaller than %dMB.' %
                      (settings.MAX_PHOTO_UPLOAD_SIZE / 1024 / 1024 - 1)))

        im = Image.open(photo)
        # Resize large images
        if any(d > 300 for d in im.size):
            im = processors.scale_and_crop(im, (300, 300), crop=True)
        fn = tempfile.mktemp(suffix='.jpg')
        f = open(fn, 'w')
        im.save(f, 'JPEG')
        f.close()
        photo.file = open(fn)
        return photo

    def clean_country(self):
        """Return a country object for the country selected (None if empty)."""
        if not self.cleaned_data['country']:
            return None

        country = Country.objects.filter(id=self.cleaned_data['country'])
        return country[0] if country else None

    def clean_groups(self):
        """Groups are saved in lowercase because it's easy and consistent."""
        if not re.match(r'^[a-zA-Z0-9 .:,-]*$', self.cleaned_data['groups']):
            raise forms.ValidationError(_(u'Tags can only contain '
                                           'alphanumeric characters, dashes, '
                                           'spaces.'))

        return [g.strip() for g in (self.cleaned_data['groups']
                                        .lower().split(','))
                if g and ',' not in g]

    def save(self, request, ldap):
        """Save this form to both LDAP and RDBMS backends, as appropriate."""
        # Save stuff in LDAP first...
        # TODO: Find out why this breaks the larper tests
        # ldap.update_person(request.user.unique_id, self.cleaned_data)
        # ldap.update_profile_photo(request.user.unique_id, self.cleaned_data)

        # ... then save other stuff in RDBMS.
        self._save_groups(request)
        profile = request.user.get_profile()
        profile.website = self.cleaned_data['website']

        address = request.user.address
        address.street = self.cleaned_data['street']
        address.city = self.cleaned_data['city']
        address.province = self.cleaned_data['province']
        address.country = self.cleaned_data['country']

        if self.cleaned_data['postal_code']:
            postal_code, created = PostalCode.objects.get_or_create(
                    code=self.cleaned_data['postal_code'])
            address.postal_code = postal_code
        else:
            address.postal_code = None

        address.save()
        profile.save()

    def _process_location_data(self):
        """Process the location data sanely so it can be saved."""
        # self.
        pass

    def _save_groups(self, request):
        """Parse a string of (usually comma-demilited) groups and save them."""
        profile = request.user.get_profile()

        # Remove any non-system groups that weren't supplied in this list.
        profile.groups.remove(*[g for g in profile.groups.all()
                                if g.name not in self.cleaned_data['groups']
                                and not g.system])

        # Add/create the rest of the groups
        groups_to_add = []
        for g in self.cleaned_data['groups']:
            (group, created) = Group.objects.get_or_create(name=g)

            if not group.system:
                groups_to_add.append(group)

        profile.groups.add(*groups_to_add)


class DeleteForm(happyforms.Form):
    unique_id = forms.CharField(widget=forms.HiddenInput)


class VouchForm(happyforms.Form):
    """Vouching is captured via a user's unique_id."""
    vouchee = forms.CharField(widget=forms.HiddenInput)


class InviteForm(happyforms.ModelForm):

    def clean_recipient(self):
        recipient = self.cleaned_data['recipient']

        if User.objects.filter(email=recipient).count() > 0:
            raise forms.ValidationError(_(u'You cannot invite someone who has '
                                            'already been vouched.'))
        return recipient

    class Meta:
        model = Invite
