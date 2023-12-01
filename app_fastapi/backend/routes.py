import fastapi
import openai
import io
import logging
import mimetypes
from pathlib import Path
from typing import AsyncGenerator
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
import aiohttp
import json
from azure.core.exceptions import ResourceNotFoundError
from starlette.responses import StreamingResponse
from fastapi import Request, HTTPException, Query, UploadFile, File, Body
from pydantic import BaseModel, Field


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


@router.post("/file/upload", tags=["Storage"], summary="文件上传")
def upload_file(file: UploadFile = File(...)):
    return {"code": 0, "message": "上传成功", "file_id": "file_id"}

@router.post("/file/delete", tags=["Storage"], summary="文件删除")
def delete_file(file_ids: list[str] = Query(...)):
    return {"code": 0, "message": "删除成功"}

@router.post("/file/list", tags=["Storage"], summary="文件查询")
def list_file(file_ids: list[str] = Query(...)):
    return {"code": 0, "message": "查询成功", "files":["file1", "file2"]}



##########################################    Agent    ##########################################

class GenerateContextResultModel(BaseModel):
    topic_description: str = Field(..., title="topic_description", description="主题描述")
    audience_description: str = Field(..., title="audience_description", description="观众描述")
    is_horizental: bool = Field(..., title="is_horizental", description="横竖屏")
    language: str = Field(..., title="language", description="语言")
    style: str = Field(..., title="style", description="风格")
    duration: float = Field(..., title="duration", description="时长")
    quantity: int = Field(..., title="quantity", description="数量")
    title_array: list[str] = Field(..., title="title_array", description="标题数组")


@router.post("/agent/generate_context", tags=["Agent"], summary="生成上下文参数")
def generate_context(assistant_id: str = Query(...), template_id: str = Query(..., description="参数内容对应的模板")):
    contextData = GenerateContextResultModel(
        topic_description="custom_topic_description",
        audience_description="custom_audience_description",
        is_horizental=False,
        language="custom_language",
        style="custom_style",
        duration=1.5,
        quantity=3,
        title_array=["custom_title1","custom_title2","custom_title3"]
    )
    return {"code": 0, "message": "生成成功", "context": contextData}

@router.post("/agent/generate_explain", tags=["Agent"], summary="生成讲解文案/互动问答")
def generate_explain(assistant_id: str = Query(...), context: GenerateContextResultModel=Body(...)):
    return {"code": 0, "message": "生成成功", "data": ["讲解或问答回复"]}



##########################################    ThirdPartyService    ##########################################

@router.post("/text_to_speech", tags=["ThirdPartyService"], summary="文字转语音")
def text_to_speech(text: str = Query(..., example = "你好"), voice_id: str = Query(..., example = "id"), only_url: bool = Query(..., example= True, description="是否仅需要url")):
    result = []
    for i in range(10):
        result.append(i)
    return {"code": 0, "message": "生成成功","audio":result, "audio_url":"audioUrl"}


@router.post("/text_to_image", tags=["ThirdPartyService"], summary="文字转图片")
def text_to_image(text: str = Query(..., example = "下雪的山水画"), only_url: bool = Query(..., example= True, description="是否仅需要url")):
    return {"code": 0, "message": "生成成功","image":[0,0,0,0,0,0], "image_url":"image_url"}



@router.post("/analyze_image", tags=["ThirdPartyService"], summary="识图")
def analyze_image(file: UploadFile = File(...), prompt: str=Query(..., example= "识别图中的弹幕", description="识图的要求")):
    return {"code": 0, "message": "生成成功","result":{}}




