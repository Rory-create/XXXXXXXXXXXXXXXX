"""
Google Drive OAuth2 authentication.

First run: opens a browser window for you to authorize.
Subsequent runs: uses the cached token.json.

To set up credentials:
  1. Visit https://console.cloud.google.com/
  2. New project → Enable "Google Drive API"
  3. Credentials → Create → OAuth 2.0 Client ID → Desktop App
  4. Download JSON → save as credentials.json in the project root
"""

import os
import logging

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config

logger = logging.getLogger(__name__)


def get_credentials() -> Credentials:
    """
    Load or refresh OAuth2 credentials.
    Returns valid Credentials object.
    Raises FileNotFoundError if credentials.json is missing.
    """
    creds = None

    if os.path.exists(config.TOKEN_FILE):
        logger.debug("Loading cached token from %s", config.TOKEN_FILE)
        creds = Credentials.from_authorized_user_file(
            config.TOKEN_FILE, config.SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired token...")
            creds.refresh(Request())
        else:
            if not os.path.exists(config.CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"credentials.json not found at '{config.CREDENTIALS_FILE}'.\n"
                    "See https://console.cloud.google.com/ to create OAuth2 "
                    "credentials for a Desktop App, then save them as credentials.json."
                )
            logger.info("Starting OAuth2 authorization flow (browser will open)...")
            flow = InstalledAppFlow.from_client_secrets_file(
                config.CREDENTIALS_FILE, config.SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(config.TOKEN_FILE, "w") as token_file:
            token_file.write(creds.to_json())
        logger.info("Token saved to %s", config.TOKEN_FILE)

    return creds


def build_drive_service(creds: Credentials):
    """Build and return an authorized Google Drive API service."""
    service = build("drive", "v3", credentials=creds)
    logger.debug("Google Drive v3 service built successfully")
    return service
