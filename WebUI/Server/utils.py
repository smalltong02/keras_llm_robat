import pydantic
from pydantic import BaseModel
from typing import Dict, Union
import httpx, os
from WebUI.configs.serverconfig import (FSCHAT_CONTROLLER, FSCHAT_OPENAI_API, FSCHAT_MODEL_WORKERS, HTTPX_DEFAULT_TIMEOUT)
import sys
import json
import asyncio
from pathlib import Path
from WebUI import workers
import urllib.request
from fastapi import FastAPI
from langchain.chat_models import ChatOpenAI, AzureChatOpenAI, ChatAnthropic
from langchain.llms import OpenAI, AzureOpenAI, Anthropic
from typing import Dict, Union, Optional, Literal, Any, List, Callable, Awaitable
from WebUI.configs.webuiconfig import *

async def wrap_done(fn: Awaitable, event: asyncio.Event):
    """Wrap an awaitable with a event to signal when it's done or an exception is raised."""
    try:
        await fn
    except Exception as e:
        # TODO: handle exception
        msg = f"Caught exception: {e}"
        print(f'{e.__class__.__name__}: {msg}')
    finally:
        # Signal the aiter to stop.
        event.set()

def fschat_controller_address() -> str:
        host = FSCHAT_CONTROLLER["host"]
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = FSCHAT_CONTROLLER["port"]
        return f"http://{host}:{port}"


def fschat_model_worker_address(model_name: str = "") -> str:
    if model := get_model_worker_config(model_name):
        host = model["host"]
        if host == "0.0.0.0":
            host = "127.0.0.1"
        port = model["port"]
        return f"http://{host}:{port}"
    return ""


def fschat_openai_api_address() -> str:
    host = FSCHAT_OPENAI_API["host"]
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = FSCHAT_OPENAI_API["port"]
    return f"http://{host}:{port}/v1"

def get_httpx_client(
        use_async: bool = False,
        proxies: Union[str, Dict] = None,
        timeout: float = HTTPX_DEFAULT_TIMEOUT,
        **kwargs,
    ) -> Union[httpx.Client, httpx.AsyncClient]:

    default_proxies = {
        # do not use proxy for locahost
        "all://127.0.0.1": None,
        "all://localhost": None,
    }
    # do not use proxy for user deployed fastchat servers
    for x in [
        fschat_controller_address(),
        fschat_model_worker_address(),
        fschat_openai_api_address(),
    ]:
        host = ":".join(x.split(":")[:2])
        default_proxies.update({host: None})

    # get proxies from system envionrent
    # proxy not str empty string, None, False, 0, [] or {}
    default_proxies.update({
        "http://": (os.environ.get("http_proxy")
                    if os.environ.get("http_proxy") and len(os.environ.get("http_proxy").strip())
                    else None),
        "https://": (os.environ.get("https_proxy")
                     if os.environ.get("https_proxy") and len(os.environ.get("https_proxy").strip())
                     else None),
        "all://": (os.environ.get("all_proxy")
                   if os.environ.get("all_proxy") and len(os.environ.get("all_proxy").strip())
                   else None),
    })
    for host in os.environ.get("no_proxy", "").split(","):
        if host := host.strip():
            default_proxies.update({host: None})

    # merge default proxies with user provided proxies
    if isinstance(proxies, str):
        proxies = {"all://": proxies}

    if isinstance(proxies, dict):
        default_proxies.update(proxies)

    # construct Client
    kwargs.update(timeout=timeout, proxies=default_proxies)
    print(kwargs)
    if use_async:
        return httpx.AsyncClient(**kwargs)
    else:
        return httpx.Client(**kwargs)
    
def set_httpx_config(timeout: float = HTTPX_DEFAULT_TIMEOUT, proxy: Union[str, Dict] = None):
    httpx._config.DEFAULT_TIMEOUT_CONFIG.connect = timeout
    httpx._config.DEFAULT_TIMEOUT_CONFIG.read = timeout
    httpx._config.DEFAULT_TIMEOUT_CONFIG.write = timeout

    proxies = {}
    if isinstance(proxy, str):
        for n in ["http", "https", "all"]:
            proxies[n + "_proxy"] = proxy
    elif isinstance(proxy, dict):
        for n in ["http", "https", "all"]:
            if p := proxy.get(n):
                proxies[n + "_proxy"] = p
            elif p := proxy.get(n + "_proxy"):
                proxies[n + "_proxy"] = p

    for k, v in proxies.items():
        os.environ[k] = v

    no_proxy = [x.strip() for x in os.environ.get("no_proxy", "").split(",") if x.strip()]
    no_proxy += [
        "http://127.0.0.1",
        "http://localhost",
    ]

    for x in [
        fschat_controller_address(),
        fschat_model_worker_address(),
        fschat_openai_api_address(),
    ]:
        host = ":".join(x.split(":")[:2])
        if host not in no_proxy:
            no_proxy.append(host)
    os.environ["NO_PROXY"] = ",".join(no_proxy)

    def _get_proxies():
        return proxies
        
    urllib.request.getproxies = _get_proxies

def get_model_path(models_list: dict = {}, model_name: str = "", type: str = None) -> Optional[str]:   
    local_paths = {}
    hugg_paths = {}
    for key, value in models_list.items():
        local_paths.update({key: value["path"]})
        hugg_paths.update({key: value["Huggingface"]})

    if path_str := local_paths.get(model_name):
        path = Path(path_str)
        if path.is_dir():
            return str(path)
    if hugg_str := hugg_paths.get(model_name):
        return hugg_str
    return ""
        
def detect_device() -> Literal["cuda", "mps", "cpu"]:
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except:
        pass
    return "cpu"
        
def llm_device(models_list: dict = {}, model_name: str = "") -> Literal["cuda", "mps", "cpu"]:
    config = models_list.get(model_name, {})
    device = config.get("device", "un")
    if device == "gpu":
        device = "cuda"
    if device not in ["cuda", "mps", "cpu"]:
        device = detect_device()
    return device

def load_8bit(models_list: dict = {}, model_name: str = "") -> bool:
    config = models_list.get(model_name, {})
    bits = config.get("loadbits", 16)
    if bits == 8:
        return True
    return False

def get_max_gpumem(models_list: dict = {}, model_name: str = "") -> str:
    config = models_list.get(model_name, {})
    memory = config.get("maxmemory", 20)
    memory_str = f"{memory}GiB"
    return memory_str

def get_model_worker_config(model_name: str = None) -> dict:
    config = {}
    configinst = InnerJsonConfigWebUIParse()
    webui_config = configinst.dump()
    server_config = webui_config.get("ServerConfig")
    
    config["host"] = server_config.get("default_host_ip")
    config["port"] = server_config["fastchat_model_worker"]["default"].get("port")
    config["vllm_enable"] = server_config["fastchat_model_worker"]["default"].get("vllm_enable")

    if model_name is None or model_name == "":
        return config

    localmodel = webui_config.get("ModelConfig").get("LocalModel")
    onlinemodel = webui_config.get("ModelConfig").get("OnlineModel")
    config.update(onlinemodel.get(model_name, {}).copy())
    if model_name in onlinemodel:
        config["online_api"] = True
        if provider := config.get("provider"):
            try:
                config["worker_class"] = getattr(workers, provider)
            except Exception as e:
                msg = f"Online Model ‘{model_name}’'s provider configuration error."
                print(f'{e.__class__.__name__}: {msg}')
    if model_name in localmodel:
        config["model_path"] = get_model_path(localmodel, model_name)
        config["device"] = llm_device(localmodel, model_name)
        config["load_8bit"] = load_8bit(localmodel, model_name)
        config["max_gpu_memory"] = get_max_gpumem(localmodel, model_name)
    return config
    # config = FSCHAT_MODEL_WORKERS.get("default", {}).copy()
    # if model_name is None or model_name == "":
    #     return config
    # configinst = InnerJsonConfigWebUIParse()
    # webui_config = configinst.dump()
    # localmodel = webui_config.get("ModelConfig").get("LocalModel")
    # onlinemodel = webui_config.get("ModelConfig").get("OnlineModel")
    # config.update(onlinemodel.get(model_name, {}).copy())
    # config.update(FSCHAT_MODEL_WORKERS.get(model_name, {}).copy())

    # if model_name in onlinemodel:
    #     config["online_api"] = True
    #     if provider := config.get("provider"):
    #         try:
    #             config["worker_class"] = getattr(workers, provider)
    #         except Exception as e:
    #             msg = f"Online Model ‘{model_name}’'s provider configuration error."
    #             print(f'{e.__class__.__name__}: {msg}')
        
    # if model_name in localmodel:
    #     config["model_path"] = get_model_path(localmodel, model_name)
    #     config["device"] = llm_device(localmodel, model_name)
    #     config["load_8bit"] = load_8bit(localmodel, model_name)
    #     config["max_gpu_memory"] = get_max_gpumem(localmodel, model_name)
    # return config

def get_vtot_worker_config(model_name: str = None) -> dict:
    config = {}
    
    configinst = InnerJsonConfigWebUIParse()
    webui_config = configinst.dump()
    server_config = webui_config.get("ServerConfig")
    config["host"] = server_config.get("default_host_ip")
    config["port"] = server_config["vtot_model_worker"].get("port")
    
    if model_name is None or model_name == "":
        return config
    vtot_model = webui_config.get("ModelConfig").get("VtoTModel")
    if model_name in vtot_model:
        config["model_path"] = vtot_model[model_name].get("path")
        config["device"] = vtot_model[model_name].get("device")
        config["loadbits"] = vtot_model[model_name].get("loadbits")
        config["Huggingface"] = vtot_model[model_name].get("Huggingface")
    return config

def MakeFastAPIOffline(
        app: FastAPI,
        static_dir=Path(__file__).parent / "static",
        static_url="/static-offline-docs",
        docs_url: Optional[str] = "/docs",
        redoc_url: Optional[str] = "/redoc",
) -> None:
    """patch the FastAPI obj that doesn't rely on CDN for the documentation page"""
    from fastapi import Request
    from fastapi.openapi.docs import (
        get_redoc_html,
        get_swagger_ui_html,
        get_swagger_ui_oauth2_redirect_html,
    )
    from fastapi.staticfiles import StaticFiles
    from starlette.responses import HTMLResponse

    openapi_url = app.openapi_url
    swagger_ui_oauth2_redirect_url = app.swagger_ui_oauth2_redirect_url

    def remove_route(url: str) -> None:
        '''
        remove original route from app
        '''
        index = None
        for i, r in enumerate(app.routes):
            if r.path.lower() == url.lower():
                index = i
                break
        if isinstance(index, int):
            app.routes.pop(index)

    # Set up static file mount
    app.mount(
        static_url,
        StaticFiles(directory=Path(static_dir).as_posix()),
        name="static-offline-docs",
    )

    if docs_url is not None:
        remove_route(docs_url)
        remove_route(swagger_ui_oauth2_redirect_url)

        # Define the doc and redoc pages, pointing at the right files
        @app.get(docs_url, include_in_schema=False)
        async def custom_swagger_ui_html(request: Request) -> HTMLResponse:
            root = request.scope.get("root_path")
            favicon = f"{root}{static_url}/favicon.png"
            return get_swagger_ui_html(
                openapi_url=f"{root}{openapi_url}",
                title=app.title + " - Swagger UI",
                oauth2_redirect_url=swagger_ui_oauth2_redirect_url,
                swagger_js_url=f"{root}{static_url}/swagger-ui-bundle.js",
                swagger_css_url=f"{root}{static_url}/swagger-ui.css",
                swagger_favicon_url=favicon,
            )

        @app.get(swagger_ui_oauth2_redirect_url, include_in_schema=False)
        async def swagger_ui_redirect() -> HTMLResponse:
            return get_swagger_ui_oauth2_redirect_html()

    if redoc_url is not None:
        remove_route(redoc_url)

        @app.get(redoc_url, include_in_schema=False)
        async def redoc_html(request: Request) -> HTMLResponse:
            root = request.scope.get("root_path")
            favicon = f"{root}{static_url}/favicon.png"

            return get_redoc_html(
                openapi_url=f"{root}{openapi_url}",
                title=app.title + " - ReDoc",
                redoc_js_url=f"{root}{static_url}/redoc.standalone.js",
                with_google_fonts=False,
                redoc_favicon_url=favicon,
            )
        
class BaseResponse(BaseModel):
    code: int = pydantic.Field(200, description="API status code")
    msg: str = pydantic.Field("success", description="API status message")
    data: Any = pydantic.Field(None, description="API data")

    class Config:
        schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
            }
        }


class ListResponse(BaseResponse):
    data: List[str] = pydantic.Field(..., description="List of names")

    class Config:
        schema_extra = {
            "example": {
                "code": 200,
                "msg": "success",
                "data": ["doc1.docx", "doc2.pdf", "doc3.txt"],
            }
        }

def get_prompt_template(type: str, name: str) -> Optional[str]:
    from WebUI.configs import prompttemplates
    import importlib
    importlib.reload(prompttemplates)
    return prompttemplates.PROMPT_TEMPLATES[type].get(name)

def get_OpenAI(
        model_name: str,
        temperature: float,
        max_tokens: int = None,
        streaming: bool = True,
        echo: bool = True,
        callbacks: List[Callable] = [],
        verbose: bool = True,
        **kwargs: Any,
) -> OpenAI:
    ## langchain model
    config_models = list_config_llm_models()
    if model_name in config_models.get("langchain", {}):
        config = config_models["langchain"][model_name]
        if model_name == "Azure-OpenAI":
            model = AzureOpenAI(
                streaming=streaming,
                verbose=verbose,
                callbacks=callbacks,
                deployment_name=config.get("deployment_name"),
                model_version=config.get("model_version"),
                openai_api_type=config.get("openai_api_type"),
                openai_api_base=config.get("api_base_url"),
                openai_api_version=config.get("api_version"),
                openai_api_key=config.get("api_key"),
                openai_proxy=config.get("openai_proxy"),
                temperature=temperature,
                max_tokens=max_tokens,
                echo=echo,
            )

        elif model_name == "OpenAI":
            model = OpenAI(
                streaming=streaming,
                verbose=verbose,
                callbacks=callbacks,
                model_name=config.get("model_name"),
                openai_api_base=config.get("api_base_url"),
                openai_api_key=config.get("api_key"),
                openai_proxy=config.get("openai_proxy"),
                temperature=temperature,
                max_tokens=max_tokens,
                echo=echo,
            )
        elif model_name == "Anthropic":
            model = Anthropic(
                streaming=streaming,
                verbose=verbose,
                callbacks=callbacks,
                model_name=config.get("model_name"),
                anthropic_api_key=config.get("api_key"),
                echo=echo,
            )
    else:
        ## fastchat model
        config = get_model_worker_config(model_name)
        model = OpenAI(
            streaming=streaming,
            verbose=verbose,
            callbacks=callbacks,
            openai_api_key=config.get("api_key", "EMPTY"),
            openai_api_base=config.get("api_base_url", fschat_openai_api_address()),
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            openai_proxy=config.get("openai_proxy"),
            echo=echo,
            **kwargs
        )

    return model

def list_embed_models() -> List[str]:
    '''
    get names of configured embedding models
    '''
    configinst = InnerJsonConfigWebUIParse()
    webui_config = configinst.dump()
    embeddingmodel = webui_config.get("ModelConfig").get("EmbeddingModel")
    return list(embeddingmodel)


def list_config_llm_models() -> Dict[str, Dict]:
    '''
    get configured llm models with different types.
    return [(model_name, config_type), ...]
    '''
    workers = list(FSCHAT_MODEL_WORKERS)
    configinst = InnerJsonConfigWebUIParse()
    webui_config = configinst.dump()
    localmodel = webui_config.get("ModelConfig").get("LocalModel")
    onlinemodel = webui_config.get("ModelConfig").get("OnlineModel")

    return {
        "local": localmodel,
        "online": onlinemodel,
        "worker": workers,
    }

def list_online_embed_models() -> List[str]:
    from WebUI.Server import model_workers

    ret = []
    for k, v in list_config_llm_models()["online"].items():
        if provider := v.get("provider"):
            worker_class = getattr(model_workers, provider, None)
            if worker_class is not None and worker_class.can_embedding():
                ret.append(k)
    return ret

def get_ChatOpenAI(
        model_name: str,
        temperature: float,
        max_tokens: int = None,
        streaming: bool = True,
        callbacks: List[Callable] = [],
        verbose: bool = True,
        **kwargs: Any,
) -> ChatOpenAI:
    ## 以下模型是Langchain原生支持的模型，这些模型不会走Fschat封装
    config_models = list_config_llm_models()

    ## 非Langchain原生支持的模型，走Fschat封装
    config = get_model_worker_config(model_name)
    model = ChatOpenAI(
        streaming=streaming,
        verbose=verbose,
        callbacks=callbacks,
        openai_api_key=config.get("api_key", "EMPTY"),
        openai_api_base=config.get("api_base_url", fschat_openai_api_address()),
        model_name=model_name,
        temperature=temperature,
        max_tokens=max_tokens,
        openai_proxy=config.get("openai_proxy"),
        **kwargs
    )

    return model
