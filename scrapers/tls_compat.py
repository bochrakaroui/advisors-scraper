"""Small helpers for machines behind SSL-inspecting proxies or custom trust chains."""

from __future__ import annotations

import logging
import os
import warnings
from typing import Any, Callable

import requests
from requests import Response, Session
from requests.exceptions import SSLError
from urllib3.exceptions import InsecureRequestWarning


TLS_RETRY_ENV_VAR = "ETF_RETRY_INSECURE_TLS"
PLAYWRIGHT_IGNORE_ENV_VAR = "ETF_IGNORE_HTTPS_ERRORS"


def _env_enabled(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def should_retry_insecure_tls() -> bool:
    return _env_enabled(TLS_RETRY_ENV_VAR, "1")


def should_ignore_playwright_https_errors() -> bool:
    return _env_enabled(PLAYWRIGHT_IGNORE_ENV_VAR, "1")


def browser_launch_args(*base_args: str) -> list[str]:
    args = list(base_args)
    if should_ignore_playwright_https_errors() and "--ignore-certificate-errors" not in args:
        args.append("--ignore-certificate-errors")
    return args


def context_https_kwargs() -> dict[str, Any]:
    return {"ignore_https_errors": should_ignore_playwright_https_errors()}


def _retry_with_insecure_tls(
    requester: Callable[..., Response],
    url: str,
    *,
    logger: logging.Logger | None = None,
    method_label: str = "GET",
    kwargs: dict[str, Any],
) -> Response:
    if not should_retry_insecure_tls():
        return requester(url, **kwargs)

    try:
        return requester(url, **kwargs)
    except SSLError as exc:
        if logger:
            logger.warning(
                "%s %s failed SSL verification; retrying once with certificate verification disabled: %s",
                method_label,
                url,
                exc,
            )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", InsecureRequestWarning)
            return requester(url, verify=False, **kwargs)


def requests_get(url: str, *, logger: logging.Logger | None = None, **kwargs: Any) -> Response:
    return _retry_with_insecure_tls(requests.get, url, logger=logger, method_label="GET", kwargs=kwargs)


def session_get(session: Session, url: str, *, logger: logging.Logger | None = None, **kwargs: Any) -> Response:
    return _retry_with_insecure_tls(session.get, url, logger=logger, method_label="GET", kwargs=kwargs)
