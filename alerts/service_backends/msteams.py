import json
import requests
from django.utils import timezone

from django import forms
from django.template.defaultfilters import truncatechars

from snappea.decorators import shared_task
from bugsink.app_settings import get_settings
from bugsink.transaction import immediate_atomic

from issues.models import Issue
from .base import BaseWebhookBackend
from .webhook_security import validate_webhook_url


class MSTeamsConfigForm(forms.Form):
    webhook_url = forms.URLField(required=True)

    def __init__(self, *args, **kwargs):
        config = kwargs.pop("config", None)

        super().__init__(*args, **kwargs)
        if config:
            self.fields["webhook_url"].initial = config.get("webhook_url", "")

    def get_config(self):
        return {
            "webhook_url": self.cleaned_data.get("webhook_url"),
        }

    def clean_webhook_url(self):
        webhook_url = self.cleaned_data["webhook_url"]
        try:
            validate_webhook_url(webhook_url)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e
        return webhook_url


def _store_failure_info(service_config_id, exception, response=None):
    """Store failure information in the MessagingServiceConfig with immediate_atomic"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)

            config.last_failure_timestamp = timezone.now()
            config.last_failure_error_type = type(exception).__name__
            config.last_failure_error_message = str(exception)

            # Handle requests-specific errors
            if response is not None:
                config.last_failure_status_code = response.status_code
                config.last_failure_response_text = response.text[:2000]  # Limit response text size

                # Check if response is JSON
                try:
                    json.loads(response.text)
                    config.last_failure_is_json = True
                except (json.JSONDecodeError, ValueError):
                    config.last_failure_is_json = False
            else:
                # Non-HTTP errors
                config.last_failure_status_code = None
                config.last_failure_response_text = None
                config.last_failure_is_json = None

            config.save()
        except MessagingServiceConfig.DoesNotExist:
            # Config was deleted while task was running
            pass


def _store_success_info(service_config_id):
    """Clear failure information on successful operation"""
    from alerts.models import MessagingServiceConfig

    with immediate_atomic(only_if_needed=True):
        try:
            config = MessagingServiceConfig.objects.get(id=service_config_id)
            config.clear_failure_status()
            config.save()
        except MessagingServiceConfig.DoesNotExist:
            # Config was deleted while task was running
            pass


def _build_adaptive_card_payload(title, facts, issue_url=None):
    body = [
        {
            "type": "TextBlock",
            "size": "Medium",
            "weight": "Bolder",
            "text": title,
            "wrap": True,
        }
    ]

    if facts:
        body.append({
            "type": "FactSet",
            "facts": [{"title": k, "value": v} for k, v in facts]
        })

    actions = []
    if issue_url:
        actions.append({
            "type": "Action.OpenUrl",
            "title": "View on Bugsink",
            "url": issue_url
        })

    card_content = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": body,
    }

    if actions:
        card_content["actions"] = actions

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card_content
            }
        ]
    }


@shared_task
def msteams_backend_send_test_message(webhook_url, project_name, display_name, service_config_id):
    facts = [
        ("Project", project_name),
        ("Message Backend", display_name),
    ]

    payload = _build_adaptive_card_payload(
        title="TEST issue - Test message by Bugsink to test the webhook setup.",
        facts=facts
    )

    try:
        result = MSTeamsBackend.safe_post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        result.raise_for_status()
        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, 'response', None)
        _store_failure_info(service_config_id, e, response)
    except Exception as e:
        _store_failure_info(service_config_id, e)


@shared_task
def msteams_backend_send_alert(
        webhook_url, issue_id, state_description, alert_article, alert_reason, service_config_id, unmute_reason=None):

    issue = Issue.objects.get(id=issue_id)
    issue_url = get_settings().BASE_URL + issue.get_absolute_url()

    title = f"{alert_reason} issue"
    issue_title = truncatechars(issue.title(), 256)

    facts = [
        ("Project", issue.project.name),
        ("Title", issue_title),
    ]

    if unmute_reason:
        facts.append(("Unmute Reason", unmute_reason))

    payload = _build_adaptive_card_payload(
        title=title,
        facts=facts,
        issue_url=issue_url
    )

    try:
        result = MSTeamsBackend.safe_post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        result.raise_for_status()
        _store_success_info(service_config_id)
    except requests.RequestException as e:
        response = getattr(e, 'response', None)
        _store_failure_info(service_config_id, e, response)
    except Exception as e:
        _store_failure_info(service_config_id, e)


class MSTeamsBackend(BaseWebhookBackend):

    def __init__(self, service_config):
        self.service_config = service_config

    @classmethod
    def get_form_class(cls):
        return MSTeamsConfigForm

    def send_test_message(self):
        config = json.loads(self.service_config.config)
        msteams_backend_send_test_message.delay(
            config["webhook_url"],
            self.service_config.project.name,
            self.service_config.display_name,
            self.service_config.id,
        )

    def send_alert(self, issue_id, state_description, alert_article, alert_reason, **kwargs):
        config = json.loads(self.service_config.config)
        msteams_backend_send_alert.delay(
            config["webhook_url"],
            issue_id,
            state_description,
            alert_article,
            alert_reason,
            self.service_config.id,
            **kwargs,
        )
