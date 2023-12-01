import fastapi
import openai
import io
import logging
import mimetypes
from pathlib import Path
from typing import AsyncGenerator
from fastapi.responses import JSONResponse
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
import aiohttp
import json
from fastapi import Request,Response
from azure.core.exceptions import ResourceNotFoundError
import os
from starlette.responses import StreamingResponse

CONFIG_OPENAI_TOKEN = "openai_token"
CONFIG_CREDENTIAL = "azure_credential"
CONFIG_ASK_APPROACH = "ask_approach"
CONFIG_CHAT_APPROACH = "chat_approach"
CONFIG_BLOB_CONTAINER_CLIENT = "blob_container_client"
CONFIG_AUTH_CLIENT = "auth_client"
CONFIG_SEARCH_CLIENT = "search_client"
ERROR_MESSAGE = """The app encountered an error processing your request.
If you are an administrator of the app, view the full error in the logs. See aka.ms/appservice-logs for more information.
Error type: {error_type}
"""
ERROR_MESSAGE_FILTER = """Your message contains content that was flagged by the OpenAI content filter."""


router = fastapi.APIRouter()


# @router.post("/ask")
@router.get("/")
async def index():
    return FileResponse('static/index.html')


# Empty page is recommended for login redirect to work.
# See https://github.com/AzureAD/microsoft-authentication-library-for-js/blob/dev/lib/msal-browser/docs/initialization.md#redirecturi-considerations for more information
@router.get("/redirect")
async def redirect():
    return ""


@router.get("/favicon.ico")
async def favicon():
    return FileResponse(str(Path(__file__).parent / "static" / "favicon.ico"))


@router.get("/assets/{path:path}")
async def assets(path: str):
    return FileResponse(str(Path(__file__).parent / "static" / "assets" / path))



# Serve content files from blob storage from within the app to keep the example self-contained.
# *** NOTE *** this assumes that the content files are public, or at least that all users of the app
# can access all the files. This is also slow and memory hungry.
@router.get("/content/{path}")
async def content_file(request: Request, path: str):
    if path.find("#page=") > 0:
        path_parts = path.rsplit("#page=", 1)
        path = path_parts[0]
    logging.info("Opening file %s at page %s", path)
    blob_container_client = getattr(request.app.state, CONFIG_BLOB_CONTAINER_CLIENT)
    try:
        blob = await blob_container_client.get_blob_client(path).download_blob()
    except ResourceNotFoundError:
        logging.exception("Path not found: %s", path)
        raise HTTPException(status_code=404)
    if not blob.properties or not blob.properties.has_key("content_settings"):
        raise HTTPException(status_code=404)
    mime_type = blob.properties["content_settings"]["content_type"]
    if mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    blob_file = io.BytesIO()
    await blob.readinto(blob_file)
    blob_file.seek(0)
    return FileResponse(blob_file, mimetype=mime_type, as_attachment=False, attachment_filename=path)


def error_dict(error: Exception) -> dict:
    if isinstance(error, openai.error.InvalidRequestError) and error.code == "content_filter":
        return {"error": ERROR_MESSAGE_FILTER}
    return {"error": ERROR_MESSAGE.format(error_type=type(error))}


def error_response(error: Exception, route: str, status_code: int = 500):
    logging.exception("Exception in %s: %s", route, error)
    if isinstance(error, openai.error.InvalidRequestError) and error.code == "content_filter":
        status_code = 400
    return JSONResponse(error_dict(error)), status_code


@router.post("/ask")
async def ask(request: Request):
    # return JSONResponse("hi")
    try:
        request_json = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=415, detail="request must be json")
   
    context = request_json.get("context", {})
    auth_helper = getattr(request.app.state, CONFIG_AUTH_CLIENT)

    context["auth_claims"] = await auth_helper.get_auth_claims_if_enabled(request.headers)
    try:
        approach = getattr(request.app.state, CONFIG_ASK_APPROACH)
        # Workaround for: https://github.com/openai/openai-python/issues/371
        async with aiohttp.ClientSession() as s:
            openai.aiosession.set(s)
            r = await approach.run(
                request_json["messages"], context=context, session_state=request_json.get("session_state")
            )
        return JSONResponse(r)
    except Exception as error:
        return error_response(error, "/ask")


async def format_as_ndjson(r: AsyncGenerator[dict, None]) -> AsyncGenerator[str, None]:
    try:
        async for event in r:
            yield json.dumps(event, ensure_ascii=False) + "\n"
    except Exception as e:
        logging.exception("Exception while generating response stream: %s", e)
        yield json.dumps(error_dict(e))


@router.post("/chat")
async def chat(request: Request):
    try:
        request_json = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "request must be json"}, status_code=415)

    context = request_json.get("context", {})
    auth_helper = getattr(request.app.state, CONFIG_AUTH_CLIENT)

    context["auth_claims"] = await auth_helper.get_auth_claims_if_enabled(request.headers)
    try:
        approach = getattr(request.app.state, CONFIG_CHAT_APPROACH)
        result = await approach.run(
            request_json["messages"],
            stream=request_json.get("stream", False),
            context=context,
            session_state=request_json.get("session_state"),
        )
        if isinstance(result, dict):
            return JSONResponse(result)
        # if isinstance(result, AsyncGenerator):
        else:
            async def generate():
                async for item in result:
                    yield json.dumps(item).encode('utf-8') + b'\n' # Add newline after each JSON item
            # Return as streaming response
            return StreamingResponse(generate(), media_type="application/x-ndjson")
        # else:
        #     response = Response(content=format_as_ndjson(result))
        #     response.headers["Content-Type"] = "application/json-lines"
        #     return response
    except Exception as error:
        return error_response(error, "/chat")


# Send MSAL.js settings to the client UI
@router.get("/auth_setup")
def auth_setup(request: Request):
    # return JSONResponse("hi")
    auth_helper = getattr(request.app.state, CONFIG_AUTH_CLIENT)
    return JSONResponse(auth_helper.get_auth_setup_for_client())




