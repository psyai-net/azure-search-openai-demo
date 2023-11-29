import os
import fastapi
from . import routes

import io
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import AsyncGenerator

import aiohttp
import openai
from azure.core.exceptions import ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential
from azure.monitor.opentelemetry import configure_azure_monitor
from azure.search.documents.aio import SearchClient
from azure.storage.blob.aio import BlobServiceClient
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware


from approaches.chatreadretrieveread import ChatReadRetrieveReadApproach
from approaches.retrievethenread import RetrieveThenReadApproach
from core.authentication import AuthenticationHelper



def create_app():
    # Check for an environment variable that's only set in production
    if os.getenv("SCM_DO_BUILD_DURING_DEPLOYMENT"):
        app = fastapi.FastAPI(
            servers=[{"url": "/api", "description": "API"}],
            root_path="/public",
            root_path_in_servers=False,
        )
    else:
        app = fastapi.FastAPI()

    @app.on_event("startup")
    async def setup_clients():
        AZURE_STORAGE_ACCOUNT = os.environ["AZURE_STORAGE_ACCOUNT"]
        AZURE_STORAGE_CONTAINER = os.environ["AZURE_STORAGE_CONTAINER"]
        AZURE_SEARCH_SERVICE = os.environ["AZURE_SEARCH_SERVICE"]
        AZURE_SEARCH_INDEX = os.environ["AZURE_SEARCH_INDEX"]
        # Shared by all OpenAI deployments
        OPENAI_HOST = os.getenv("OPENAI_HOST", "azure")
        OPENAI_CHATGPT_MODEL = os.environ["AZURE_OPENAI_CHATGPT_MODEL"]
        OPENAI_EMB_MODEL = os.getenv("AZURE_OPENAI_EMB_MODEL_NAME", "text-embedding-ada-002")
        # Used with Azure OpenAI deployments
        AZURE_OPENAI_SERVICE = os.getenv("AZURE_OPENAI_SERVICE")
        AZURE_OPENAI_CHATGPT_DEPLOYMENT = os.getenv("AZURE_OPENAI_CHATGPT_DEPLOYMENT")
        AZURE_OPENAI_EMB_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMB_DEPLOYMENT")
        # Used only with non-Azure OpenAI deployments
        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        OPENAI_ORGANIZATION = os.getenv("OPENAI_ORGANIZATION")
        AZURE_USE_AUTHENTICATION = os.getenv("AZURE_USE_AUTHENTICATION", "").lower() == "true"
        AZURE_SERVER_APP_ID = os.getenv("AZURE_SERVER_APP_ID")
        AZURE_SERVER_APP_SECRET = os.getenv("AZURE_SERVER_APP_SECRET")
        AZURE_CLIENT_APP_ID = os.getenv("AZURE_CLIENT_APP_ID")
        AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
        TOKEN_CACHE_PATH = os.getenv("TOKEN_CACHE_PATH")

        KB_FIELDS_CONTENT = os.getenv("KB_FIELDS_CONTENT", "content")
        KB_FIELDS_SOURCEPAGE = os.getenv("KB_FIELDS_SOURCEPAGE", "sourcepage")

        AZURE_SEARCH_QUERY_LANGUAGE = os.getenv("AZURE_SEARCH_QUERY_LANGUAGE", "en-us")
        AZURE_SEARCH_QUERY_SPELLER = os.getenv("AZURE_SEARCH_QUERY_SPELLER", "lexicon")

        # Use the current user identity to authenticate with Azure OpenAI, AI Search and Blob Storage (no secrets needed,
        # just use 'az login' locally, and managed identity when deployed on Azure). If you need to use keys, use separate AzureKeyCredential instances with the
        # keys for each service
        # If you encounter a blocking error during a DefaultAzureCredential resolution, you can exclude the problematic credential by using a parameter (ex. exclude_shared_token_cache_credential=True)
        azure_credential = DefaultAzureCredential(exclude_shared_token_cache_credential=True)

        # Set up authentication helper
        auth_helper = AuthenticationHelper(
            use_authentication=AZURE_USE_AUTHENTICATION,
            server_app_id=AZURE_SERVER_APP_ID,
            server_app_secret=AZURE_SERVER_APP_SECRET,
            client_app_id=AZURE_CLIENT_APP_ID,
            tenant_id=AZURE_TENANT_ID,
            token_cache_path=TOKEN_CACHE_PATH,
        )

        # Set up clients for AI Search and Storage
        search_client = SearchClient(
            endpoint=f"https://{AZURE_SEARCH_SERVICE}.search.windows.net",
            index_name=AZURE_SEARCH_INDEX,
            credential=azure_credential,
        )
        blob_client = BlobServiceClient(
            account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net", credential=azure_credential
        )
        blob_container_client = blob_client.get_container_client(AZURE_STORAGE_CONTAINER)

        # Used by the OpenAI SDK
        if OPENAI_HOST == "azure":
            openai.api_type = "azure_ad"
            openai.api_base = f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com"
            openai.api_version = "2023-07-01-preview"
            openai_token = await azure_credential.get_token("https://cognitiveservices.azure.com/.default")
            openai.api_key = openai_token.token
            # Store on app.config for later use inside requests
            app.state[routes.CONFIG_OPENAI_TOKEN] = openai_token
        else:
            openai.api_type = "openai"
            openai.api_key = OPENAI_API_KEY
            openai.organization = OPENAI_ORGANIZATION

        app.state[routes.CONFIG_CREDENTIAL] = azure_credential
        app.state[routes.CONFIG_SEARCH_CLIENT] = search_client
        app.state[routes.CONFIG_BLOB_CONTAINER_CLIENT] = blob_container_client
        app.state[routes.CONFIG_AUTH_CLIENT] = auth_helper

        # Various approaches to integrate GPT and external knowledge, most applications will use a single one of these patterns
        # or some derivative, here we include several for exploration purposes
        app.state[routes.CONFIG_ASK_APPROACH] = RetrieveThenReadApproach(
            search_client,
            OPENAI_HOST,
            AZURE_OPENAI_CHATGPT_DEPLOYMENT,
            OPENAI_CHATGPT_MODEL,
            AZURE_OPENAI_EMB_DEPLOYMENT,
            OPENAI_EMB_MODEL,
            KB_FIELDS_SOURCEPAGE,
            KB_FIELDS_CONTENT,
            AZURE_SEARCH_QUERY_LANGUAGE,
            AZURE_SEARCH_QUERY_SPELLER,
        )

        app.state[routes.CONFIG_CHAT_APPROACH] = ChatReadRetrieveReadApproach(
            search_client,
            OPENAI_HOST,
            AZURE_OPENAI_CHATGPT_DEPLOYMENT,
            OPENAI_CHATGPT_MODEL,
            AZURE_OPENAI_EMB_DEPLOYMENT,
            OPENAI_EMB_MODEL,
            KB_FIELDS_SOURCEPAGE,
            KB_FIELDS_CONTENT,
            AZURE_SEARCH_QUERY_LANGUAGE,
            AZURE_SEARCH_QUERY_SPELLER,
        )

    @app.middleware("http")
    async def ensure_openai_token():
        if openai.api_type != "azure_ad":
            return
        openai_token = app.state[routes.CONFIG_OPENAI_TOKEN]
        if openai_token.expires_on < time.time() + 60:
            openai_token = await app.state[routes.CONFIG_CREDENTIAL].get_token(
                "https://cognitiveservices.azure.com/.default"
            )
            app.state[routes.CONFIG_OPENAI_TOKEN] = openai_token
            openai.api_key = openai_token.token

    app.include_router(routes.router)
    return app
