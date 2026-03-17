from fastapi import APIRouter
from app.model_registry import get_models_list
from app.nexos_client import _get_cookies
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/v1/models")
async def list_models():
    try:
        cookies = _get_cookies()
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    models = await get_models_list(cookies)
    return {"object": "list", "data": models}
