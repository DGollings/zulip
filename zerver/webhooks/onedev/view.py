from typing import Dict, List, Optional, Protocol

from django.http import HttpRequest, HttpResponse

import re2

from zerver.decorator import webhook_view
from zerver.lib.exceptions import UnsupportedWebhookEventType
from zerver.lib.request import REQ, has_request_variables
from zerver.lib.response import json_success
from zerver.lib.validator import (
    WildValue,
    check_bool,
    check_int,
    check_none_or,
    check_string,
    to_wild_value,
)
from zerver.lib.webhooks.common import (
    check_send_webhook_message,
    get_http_headers_from_filename,
    validate_extract_webhook_http_header,
)
from zerver.lib.webhooks.git import (
    TOPIC_WITH_BRANCH_TEMPLATE,
    TOPIC_WITH_PR_OR_ISSUE_INFO_TEMPLATE,
    TOPIC_WITH_RELEASE_TEMPLATE,
    get_create_branch_event_message,
    get_issue_event_message,
    get_pull_request_event_message,
    get_push_commits_event_message,
    get_release_event_message,
)
from zerver.models import UserProfile

# ALL_EVENT_TYPES = ["issue_comment", "issues", "create", "pull_request", "push", "release"]
ALL_EVENT_TYPES = ["issue_opened", "issue_changed", "pull_request_opened", "pull_request_changed"]



@webhook_view("Onedev", all_event_types=ALL_EVENT_TYPES)
@has_request_variables
def api_onedev_webhook(
    request: HttpRequest,
    user_profile: UserProfile,
    payload: WildValue = REQ(argument_type="body", converter=to_wild_value),
    branches: Optional[str] = REQ(default=None),
    user_specified_topic: Optional[str] = REQ("topic", default=None),
) -> HttpResponse:
    return onedev_webhook_main(
        "Onedev",
        "X-Gogs-Event",
        request,
        user_profile,
        payload,
        branches,
        user_specified_topic,
    )


def get_event(payload: WildValue):
    rawEvent = payload["@class"].tame(check_string)
    if rawEvent == "io.onedev.server.event.issue.IssueOpened":
        return "issue_opened"
    elif rawEvent == "io.onedev.server.event.issue.IssueChanged":
        return "issue_changed"
    elif rawEvent == "io.onedev.server.event.pullrequest.PullRequestOpened":
        return "pull_request_opened"
    elif rawEvent == "io.onedev.server.event.pullrequest.PullRequestChanged":
        return "pull_request_changed"


def patchURL(text: str):
    '''currently onedev has no url, this replaces URls with target None with simply the URL title'''
    return re2.sub(
        "\[(.+)\]\(None\)",
        lambda match_obj: match_obj.group(1) if match_obj.group(1) is not None else match_obj(0),
        text,
    )


def format_issue_topic(payload: WildValue) -> str:
    project_name = payload["project"]["name"].tame(check_string)
    number = payload["issue"]["number"].tame(check_int)
    title = payload["issue"]["title"].tame(check_string)
    return f"{project_name} / issue #{number} {title}"


def format_issue_event(payload: WildValue, event_type: str) -> str:
    user_name = payload["user"]["name"].tame(check_string)
    number = payload["issue"]["number"].tame(check_int)
    title = payload["issue"]["title"].tame(check_string)
    if event_type == "opened":
        message = payload["issue"]["description"].tame(check_none_or(check_string))  # TODO tame?
    else:
        change_type = payload["change"]["data"]["@type"].tame(check_string)
        if change_type == "IssueStateChangeData":
            old_state = payload["change"]["data"]["oldState"].tame(check_string)
            new_state = payload["change"]["data"]["newState"].tame(check_string)
            message = f"{user_name} changed 'state' from '{old_state}' to '{new_state}'"
    return patchURL(
        get_issue_event_message(
            url=None,
            user_name=user_name,
            number=number,
            action=event_type,
            title=title,
            message=message,
        )
    )

def format_pull_request_topic(payload: WildValue) -> str:
    project_name = payload["project"]["name"].tame(check_string)
    number = payload["request"]["number"].tame(check_int)
    title = payload["request"]["title"].tame(check_string)
    return f"{project_name} / PR #{number} {title}"

        # expected_message = """john opened [PR #1](http://localhost:3000/john/try-git/pulls/1) from `feature` to `master`."""
def format_pull_request_event(payload: WildValue, include_title: bool = False) -> str:
    user_name = payload["user"]["name"].tame(check_string)
    action = payload["request"]["lastUpdate"]["activity"].tame(check_string)
    url = None
    number = payload["request"]["number"].tame(check_int)
    target_branch = payload["request"]["sourceBranch"].tame(check_string) # sourceBranch/targetBranch seems to be reversed vs target/base_branch
    base_branch = payload["request"]["targetBranch"].tame(check_string)
    title = payload["pull_request"]["title"].tame(check_string) if include_title else None

    return patchURL(get_pull_request_event_message(
        user_name=user_name,
        action=action,
        url=url,
        number=number,
        target_branch=target_branch,
        base_branch=base_branch,
        title=title,
    ))

def onedev_webhook_main(
    integration_name: str,
    http_header_name: str,
    request: HttpRequest,
    user_profile: UserProfile,
    payload: WildValue,
    branches: Optional[str],
    user_specified_topic: Optional[str],
) -> HttpResponse:
    repo = payload["project"]["name"].tame(check_string)
    event = get_event(payload)
    if event == "issue_opened":
        event_type = event.split("_")[1]
        body = format_issue_event(payload, event_type)
        if user_specified_topic:
            topic = user_specified_topic
        else:
            topic = format_issue_topic(payload)
    elif event == "issue_changed":
        event_type = event.split("_")[1]
        body = format_issue_event(payload, event_type)
        if user_specified_topic:
            topic = user_specified_topic
        else:
            topic = format_issue_topic(payload)
    elif event == "pull_request_opened":
        body = format_pull_request_event(payload)
        if user_specified_topic:
            topic = user_specified_topic
        else:
            topic = format_pull_request_topic(payload)
    elif event == "pull_request_changed":
        body = format_pull_request_event(payload)
        if user_specified_topic:
            topic = user_specified_topic
        else:
            topic = format_pull_request_topic(payload)

    # elif event == "create":
    #     body = format_new_branch_event(payload)
    #     topic = TOPIC_WITH_BRANCH_TEMPLATE.format(
    #         repo=repo,
    #         branch=payload["ref"].tame(check_string),
    #     )
    # elif event == "pull_request":
    #     body = format_pull_request_event(
    #         payload,
    #         include_title=user_specified_topic is not None,
    #     )
    #     topic = TOPIC_WITH_PR_OR_ISSUE_INFO_TEMPLATE.format(
    #         repo=repo,
    #         type="PR",
    #         id=payload["pull_request"]["id"].tame(check_int),
    #         title=payload["pull_request"]["title"].tame(check_string),
    #     )
    # elif event == "issues":
    #     body = format_issues_event(
    #         payload,
    #         include_title=user_specified_topic is not None,
    #     )
    #     topic = TOPIC_WITH_PR_OR_ISSUE_INFO_TEMPLATE.format(
    #         repo=repo,
    #         type="issue",
    #         id=payload["issue"]["number"].tame(check_int),
    #         title=payload["issue"]["title"].tame(check_string),
    #     )
    # elif event == "issue_comment":
    #     body = format_issue_comment_event(
    #         payload,
    #         include_title=user_specified_topic is not None,
    #     )
    #     topic = TOPIC_WITH_PR_OR_ISSUE_INFO_TEMPLATE.format(
    #         repo=repo,
    #         type="issue",
    #         id=payload["issue"]["number"].tame(check_int),
    #         title=payload["issue"]["title"].tame(check_string),
    #     )
    # elif event == "release":
    #     body = format_release_event(
    #         payload,
    #         include_title=user_specified_topic is not None,
    #     )
    #     topic = TOPIC_WITH_RELEASE_TEMPLATE.format(
    #         repo=repo,
    #         tag=payload["release"]["tag_name"].tame(check_string),
    #         title=payload["release"]["name"].tame(check_string),
    #     )

    else:
        raise UnsupportedWebhookEventType(event)

    check_send_webhook_message(request, user_profile, topic, body, event)
    return json_success(request)
