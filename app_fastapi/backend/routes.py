import fastapi

router = fastapi.APIRouter()


@router.post("/ask")
async def ask():

    return "hello"


























