"""
edX API classes which call edX service REST API endpoints using the edx-rest-api-client module.
"""
import logging

import backoff
from six import text_type
from slumber.exceptions import HttpClientError, HttpServerError, HttpNotFoundError

from edx_rest_api_client.client import EdxRestApiClient


LOG = logging.getLogger(__name__)

OAUTH_ACCESS_TOKEN_URL = "/oauth2/access_token"


class BaseApiClient(object):
    """
    API client base class used to submit API requests to a particular web service.
    """
    append_slash = True
    _client = None

    def __init__(self, lms_base_url, api_base_url, client_id, client_secret):
        """
        Retrieves OAuth access token from the LMS and creates REST API client instance.
        """
        self.api_base_url = api_base_url
        access_token, __ = self.get_access_token(lms_base_url, client_id, client_secret)
        self.create_client(access_token)

    def create_client(self, access_token):
        """
        Creates and stores the EdxRestApiClient that we use to actually make requests.
        """
        self._client = EdxRestApiClient(
            self.api_base_url,
            jwt=access_token,
            append_slash=self.append_slash
        )

    @staticmethod
    def get_access_token(oauth_base_url, client_id, client_secret):
        """
        Returns an access token and expiration date from the OAuth provider.

        Returns:
            (str, datetime)
        """
        try:
            return EdxRestApiClient.get_oauth_access_token(
                oauth_base_url + OAUTH_ACCESS_TOKEN_URL, client_id, client_secret, token_type='jwt'
            )
        except HttpClientError as err:
            LOG.error("API Error: {}".format(err.content))
            raise


def _backoff_handler(details):
    """
    Simple logging handler for when timeout backoff occurs.
    """
    LOG.info('Trying again in {wait:0.1f} seconds after {tries} tries calling {target}'.format(**details))


def _exception_not_like(statuses=None):
    """
    Parameterized callback for backoff's "giveup" argument which checks that the exception does NOT have any of the
    given statuses.
    """
    def inner(exc):  # pylint: disable=missing-docstring
        return exc.response.status_code not in statuses
    return inner


def _retry_lms_api(retry_statuses=None):
    """
    Decorator which enables retries with sane backoff defaults for LMS APIs.
    """
    # At the very least, retry on 504 response status which is used by Nginx/gunicorn to indicate backend python workers
    # are occupied or otherwise unavailable.
    if not retry_statuses:
        retry_statuses = [504]
    elif 504 not in retry_statuses:
        retry_statuses.append(504)

    def inner(func):  # pylint: disable=missing-docstring
        func_with_backoff = backoff.on_exception(
            backoff.expo,
            HttpServerError,
            max_time=600,  # 10 minutes
            giveup=_exception_not_like(statuses=retry_statuses),
            # Wrap the actual _backoff_handler so that we can patch the real one in unit tests.  Otherwise, the func
            # will get decorated on import, embedding this handler as a python object reference, precluding our ability
            # to patch it in tests.
            on_backoff=lambda details: _backoff_handler(details)  # pylint: disable=unnecessary-lambda
        )(func)
        return func_with_backoff
    return inner


class LmsApi(BaseApiClient):
    """
    LMS API client with convenience methods for making API calls.
    """
    @_retry_lms_api()
    def learners_to_retire(self, states_to_request, cool_off_days=7):
        """
        Retrieves a list of learners awaiting retirement actions.
        """
        params = {
            'cool_off_days': cool_off_days,
            'states': states_to_request
        }
        try:
            return self._client.api.user.v1.accounts.retirement_queue.get(**params)
        except HttpClientError as err:
            try:
                LOG.error("API Error: {}".format(err.content))
            except AttributeError:
                LOG.error("API Error: {}".format(text_type(err)))
            raise err

    @_retry_lms_api()
    def get_learner_retirement_state(self, username):
        """
        Retrieves the given learner's retirement state.
        """
        return self._client.api.user.v1.accounts(username).retirement_status.get()

    @_retry_lms_api()
    def update_learner_retirement_state(self, username, new_state_name, message):
        """
        Updates the given learner's retirement state to the retirement state name new_string
        with the additional string information in message (for logging purposes).
        """
        params = {
            'data': {
                'username': username,
                'new_state': new_state_name,
                'response': message
            },
        }

        return self._client.api.user.v1.accounts.update_retirement_status.patch(**params)

    @_retry_lms_api()
    def retirement_deactivate_logout(self, learner):
        """
        Performs the user deactivation and forced logout step of learner retirement
        """
        params = {'data': {'username': learner['original_username']}}
        return self._client.api.user.v1.accounts.deactivate_logout.post(**params)

    @_retry_lms_api()
    def retirement_retire_forum(self, learner):
        """
        Performs the forum retirement step of learner retirement
        """
        # api/discussion/
        params = {'data': {'username': learner['original_username']}}
        try:
            return self._client.api.discussion.v1.accounts.retire_forum.post(**params)
        except HttpNotFoundError:
            return True

    @_retry_lms_api(retry_statuses=[500, 504])
    def retirement_retire_mailings(self, learner):
        """
        Performs the email list retirement step of learner retirement
        """
        params = {'data': {'username': learner['original_username']}}
        return self._client.api.user.v1.accounts.retire_mailings.post(**params)

    @_retry_lms_api()
    def retirement_unenroll(self, learner):
        """
        Unenrolls the user from all courses
        """
        params = {'data': {'username': learner['original_username']}}
        return self._client.api.enrollment.v1.unenroll.post(**params)

    # This endpoint additionaly returns 500 when the EdxNotes backend service is unavailable.
    @_retry_lms_api(retry_statuses=[500, 504])
    def retirement_retire_notes(self, learner):
        """
        Deletes all the user's notes (aka. annotations)
        """
        params = {'data': {'username': learner['original_username']}}
        return self._client.api.edxnotes.v1.retire_user.post(**params)

    @_retry_lms_api()
    def retirement_lms_retire_misc(self, learner):
        """
        Deletes, blanks, or one-way hashes personal information in LMS as
        defined in EDUCATOR-2802 and sub-tasks.
        """
        params = {'data': {'username': learner['original_username']}}
        return self._client.api.user.v1.accounts.retire_misc.post(**params)

    @_retry_lms_api()
    def retirement_lms_retire(self, learner):
        """
        Deletes, blanks, or one-way hashes all remaining personal information in LMS
        """
        params = {'data': {'username': learner['original_username']}}
        return self._client.api.user.v1.accounts.retire.post(**params)


class EcommerceApi(BaseApiClient):
    """
    Ecommerce API client with convenience methods for making API calls.
    """
    @_retry_lms_api()
    def retire_learner(self, learner):
        """
        Performs the learner retirement step for Ecommerce
        """
        params = {'data': {'username': learner['original_username']}}
        return self._client.api.v2.user.retire.post(**params)


class CredentialsApi(BaseApiClient):
    """
    Credentials API client with convenience methods for making API calls.
    """
    @_retry_lms_api()
    def retire_learner(self, learner):
        """
        Performs the learner retiement step for Credentials
        """
        params = {'data': {'username': learner['original_username']}}
        return self._client.user.retire.post(**params)