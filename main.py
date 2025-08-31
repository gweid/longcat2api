import os
import json
import random
import time
import asyncio
import uuid
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
import aiohttp
from aiohttp import web
import logging

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== 配置 ====================
@dataclass
class Config:
    """应用配置"""
    API_BASEURL: str = os.getenv("API_BASEURL", "https://longcat.chat")
    PORT: int = int(os.getenv("PORT", "3011"))
    DELAY_MIN: float = 3.0
    DELAY_MAX: float = 5.0
    
    # 浏览器 User-Agent 列表
    USER_AGENTS: List[str] = None
    
    def __post_init__(self):
        if self.USER_AGENTS is None:
            self.USER_AGENTS = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ]

config = Config()

# ==================== 工具函数 ====================
def get_random_delay() -> float:
    """获取随机延迟时间"""
    return config.DELAY_MIN + random.random() * (config.DELAY_MAX - config.DELAY_MIN)

def parse_auth_cookies(auth_header: Optional[str]) -> Optional[List[str]]:
    """解析 Authorization 头获取 cookies"""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    
    token = auth_header[7:].strip()
    if token.lower() in ["false", "null", "none"]:
        return None
    
    return [c.strip() for c in token.split(",") if c.strip()]

def format_messages(messages: List[Dict[str, str]]) -> str:
    """格式化消息为 LongCat 格式"""
    return ";".join(f"{msg['role']}:{msg['content']}" for msg in messages)

def generate_chat_id() -> str:
    """生成聊天完成 ID"""
    return f"chatcmpl-{str(uuid.uuid4())}"

def create_headers(cookie: str, user_agent: str, referer: str = None) -> Dict[str, str]:
    """创建通用请求头"""
    headers = {
        "User-Agent": user_agent,
        "Content-Type": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "X-Client-Language": "zh",
        "Cookie": f"passport_token_key={cookie}"
    }
    if referer:
        headers["Referer"] = referer
    else:
        headers["Referer"] = f"{config.API_BASEURL}/"
    return headers

# ==================== 会话管理 ====================
class SessionManager:
    """会话管理器"""
    
    @staticmethod
    async def create(cookie: str, user_agent: str, session: aiohttp.ClientSession) -> str:
        """创建会话"""
        headers = create_headers(cookie, user_agent)
        data = {"model": "", "agentId": ""}
        
        async with session.post(
            f"{config.API_BASEURL}/api/v1/session-create",
            headers=headers,
            json=data
        ) as response:
            if response.status != 200:
                raise Exception(f"会话创建失败: {response.status}")
            
            data = await response.json()
            if data.get("code") != 0:
                raise Exception(f"会话创建错误: {data.get('message')}")
            
            return data["data"]["conversationId"]
    
    @staticmethod
    async def delete(conversation_id: str, cookie: str, user_agent: str) -> None:
        """删除会话（带随机延迟）"""
        try:
            delay = get_random_delay()
            logger.info(f"等待 {delay:.0f}秒 后删除会话 {conversation_id}")
            await asyncio.sleep(delay)
            
            headers = create_headers(cookie, user_agent, f"{config.API_BASEURL}/c/{conversation_id}")
            
            async with aiohttp.ClientSession() as session:
                url = f"{config.API_BASEURL}/api/v1/session-delete?conversationId={conversation_id}"
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        logger.info(f"成功删除会话 {conversation_id}")
                    else:
                        logger.error(f"删除会话失败: {response.status}")
        except Exception as e:
            logger.error(f"删除会话出错: {e}")
    
    @staticmethod
    def schedule_deletion(conversation_id: str, cookie: str, user_agent: str):
        """异步删除会话（不等待）"""
        asyncio.create_task(SessionManager.delete(conversation_id, cookie, user_agent))

# ==================== SSE 处理 ====================
class SSEProcessor:
    """SSE (Server-Sent Events) 处理器"""
    
    @staticmethod
    def parse_line(line: str) -> Optional[Dict[str, Any]]:
        """解析 SSE 数据行"""
        if not line.startswith("data:"):
            return None
        
        json_str = line[5:].strip()
        if not json_str or json_str == "[DONE]":
            return None
        
        try:
            return json.loads(json_str)
        except:
            return None
    
    @staticmethod
    def create_chunk(model: str, content: str = None, role: str = None,
                    finish_reason: str = None, chunk_id: str = None) -> Dict[str, Any]:
        """创建响应块"""
        chunk = {
            "id": chunk_id or generate_chat_id(),
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason
            }]
        }
        
        if role:
            chunk["choices"][0]["delta"]["role"] = role
        if content:
            chunk["choices"][0]["delta"]["content"] = content
            
        return chunk
    
    @staticmethod
    async def process_buffer(buffer: str, lines: List[str]) -> Tuple[str, List[Dict[str, Any]]]:
        """处理缓冲区数据"""
        parsed_data = []
        for line in lines[:-1]:
            data = SSEProcessor.parse_line(line)
            if data:
                parsed_data.append(data)
        return lines[-1], parsed_data  # 返回剩余缓冲区和解析的数据

# ==================== 响应处理器 ====================
class ResponseHandler:
    """处理流式和非流式响应"""
    
    @staticmethod
    async def handle_non_stream(response: aiohttp.ClientResponse, model: str) -> str:
        """处理非流式响应，返回完整内容"""
        full_content = ""
        buffer = ""
        
        async for chunk in response.content.iter_any():
            buffer += chunk.decode('utf-8', errors='ignore')
            lines = buffer.split("\n")
            buffer, parsed_data = await SSEProcessor.process_buffer(buffer, lines)
            
            for data in parsed_data:
                if data.get("choices") and data["choices"][0].get("delta", {}).get("content"):
                    full_content += data["choices"][0]["delta"]["content"]
        
        # 处理剩余缓冲区
        if buffer:
            data = SSEProcessor.parse_line(buffer)
            if data and data.get("choices") and data["choices"][0].get("delta", {}).get("content"):
                full_content += data["choices"][0]["delta"]["content"]
        
        return full_content
    
    @staticmethod
    def create_completion_response(model: str, content: str) -> Dict[str, Any]:
        """创建非流式完成响应"""
        return {
            "id": generate_chat_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }
    
    @staticmethod
    async def handle_stream(
        request: web.Request,
        response: aiohttp.ClientResponse,
        model: str,
        conversation_id: str,
        cookie: str,
        user_agent: str
    ) -> web.StreamResponse:
        """处理流式响应"""
        buffer = ""
        is_first_chunk = True
        session_deleted = False
        
        # 创建流式响应
        stream_response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
        await stream_response.prepare(request)
        
        try:
            async for chunk in response.content.iter_any():
                buffer += chunk.decode('utf-8', errors='ignore')
                lines = buffer.split("\n")
                buffer, parsed_data = await SSEProcessor.process_buffer(buffer, lines)
                
                for data in parsed_data:
                    await ResponseHandler._process_stream_data(
                        stream_response, data, model, is_first_chunk
                    )
                    
                    if is_first_chunk and data.get("choices") and data["choices"][0].get("delta", {}).get("role"):
                        is_first_chunk = False
                    
                    # 处理结束标记
                    if data.get("lastOne") is True:
                        await stream_response.write(b"data: [DONE]\n\n")
                        if not session_deleted:
                            session_deleted = True
                            SessionManager.schedule_deletion(conversation_id, cookie, user_agent)
            
            # 处理剩余缓冲区
            if buffer:
                data = SSEProcessor.parse_line(buffer)
                if data:
                    await ResponseHandler._process_stream_data(stream_response, data, model, False)
            
            # 确保发送结束标记
            await stream_response.write(b"data: [DONE]\n\n")
            
            if not session_deleted:
                SessionManager.schedule_deletion(conversation_id, cookie, user_agent)
                
        except Exception as e:
            logger.error(f"流处理错误: {e}")
            await stream_response.write(b"data: [DONE]\n\n")
            if not session_deleted:
                SessionManager.schedule_deletion(conversation_id, cookie, user_agent)
        
        await stream_response.write_eof()
        return stream_response
    
    @staticmethod
    async def _process_stream_data(
        stream_response: web.StreamResponse,
        data: Dict[str, Any],
        model: str,
        is_first_chunk: bool
    ):
        """处理单个流数据块"""
        if not data.get("choices"):
            return
        
        choice = data["choices"][0]
        delta = choice.get("delta", {})
        chunk_id = f"chatcmpl-{data.get('id', str(uuid.uuid4()))}"
        
        # 处理角色信息
        if is_first_chunk and delta.get("role"):
            chunk = SSEProcessor.create_chunk(model, role="assistant", chunk_id=chunk_id)
            await stream_response.write(f"data: {json.dumps(chunk)}\n\n".encode('utf-8'))
        
        # 处理内容
        if delta.get("content"):
            chunk = SSEProcessor.create_chunk(model, content=delta["content"], chunk_id=chunk_id)
            await stream_response.write(f"data: {json.dumps(chunk)}\n\n".encode('utf-8'))
        
        # 处理结束
        if data.get("finishReason") == "stop":
            chunk = SSEProcessor.create_chunk(model, finish_reason="stop", chunk_id=chunk_id)
            await stream_response.write(f"data: {json.dumps(chunk)}\n\n".encode('utf-8'))
    

# ==================== API 处理器 ====================
class APIHandler:
    """API 请求处理器"""
    
    @staticmethod
    async def handle_models(request: web.Request) -> web.Response:
        """处理模型列表请求"""
        return web.json_response({
            "object": "list",
            "data": [
                {
                  "id": "longcat-flash",
                  "object": "model",
                  "created": int(time.time()),
                  "owned_by": "longcat"
                },
                {
                    "id": "LongCat",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "longcat"
                },
                {
                    "id": "LongCat-Search",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "longcat"
                }
            ]
        })
    
    @staticmethod
    async def handle_chat_completions(request: web.Request) -> web.Response:
        """处理聊天完成请求"""
        conversation_id = None
        selected_cookie = None
        user_agent = None
        
        try:
            # 解析请求
            body = await request.json()
            messages = body.get("messages", [])
            stream = body.get("stream", False)
            model = body.get("model", "LongCat")
            
            # 验证授权
            auth_header = request.headers.get("Authorization")
            user_cookies = parse_auth_cookies(auth_header)
            
            if not user_cookies:
                return APIHandler._error_response(
                    "缺少或无效的 Authorization 头。请提供有效的 Bearer token。",
                    "authorization_error",
                    401
                )
            
            # 随机选择 cookie 和 UA
            selected_cookie = random.choice(user_cookies)
            user_agent = random.choice(config.USER_AGENTS)
            
            # 执行聊天请求
            async with aiohttp.ClientSession() as session:
                conversation_id = await SessionManager.create(selected_cookie, user_agent, session)
                
                # 准备聊天数据
                chat_response = await APIHandler._send_chat_request(
                    session, conversation_id, messages, model, selected_cookie, user_agent
                )
                
                # 处理响应
                if stream:
                    return await ResponseHandler.handle_stream(
                        request, chat_response, model, conversation_id, selected_cookie, user_agent
                    )
                else:
                    content = await ResponseHandler.handle_non_stream(chat_response, model)
                    SessionManager.schedule_deletion(conversation_id, selected_cookie, user_agent)
                    return web.json_response(
                        ResponseHandler.create_completion_response(model, content)
                    )
        
        except Exception as e:
            # 错误时清理会话
            if conversation_id and selected_cookie and user_agent:
                SessionManager.schedule_deletion(conversation_id, selected_cookie, user_agent)
            
            logger.error(f"聊天完成请求错误: {e}")
            return APIHandler._error_response(str(e), "internal_error", 500)
    
    @staticmethod
    async def _send_chat_request(
        session: aiohttp.ClientSession,
        conversation_id: str,
        messages: List[Dict[str, str]],
        model: str,
        cookie: str,
        user_agent: str
    ) -> aiohttp.ClientResponse:
        """发送聊天请求到后端"""
        headers = create_headers(cookie, user_agent, f"{config.API_BASEURL}/c/{conversation_id}")
        headers["Accept"] = "text/event-stream,application/json"
        
        chat_data = {
            "conversationId": conversation_id,
            "content": format_messages(messages),
            "reasonEnabled": 0,
            "searchEnabled": 1 if "search" in model.lower() else 0,
            "parentMessageId": 0
        }
        
        response = await session.post(
            f"{config.API_BASEURL}/api/v1/chat-completion",
            headers=headers,
            json=chat_data
        )
        
        if response.status != 200:
            raise Exception(f"聊天请求失败: {response.status}")
        
        return response
    
    @staticmethod
    def _error_response(message: str, error_type: str, status: int) -> web.Response:
        """创建错误响应"""
        return web.json_response({
            "error": {
                "message": message,
                "type": error_type,
                "code": error_type.replace("_", "")
            }
        }, status=status)
    
    @staticmethod
    async def handle_404(request: web.Request) -> web.Response:
        """处理 404 错误"""
        return web.Response(text="Not Found", status=404)

# ==================== 应用初始化 ====================
def create_app() -> web.Application:
    """创建并配置应用"""
    app = web.Application()
    
    # 注册路由
    app.router.add_get("/v1/models", APIHandler.handle_models)
    app.router.add_post("/v1/chat/completions", APIHandler.handle_chat_completions)
    app.router.add_route("*", "/{path:.*}", APIHandler.handle_404)
    
    return app

# ==================== 主入口 ====================
if __name__ == "__main__":
    app = create_app()
    logger.info(f"启动服务器，端口: {config.PORT}")
    web.run_app(app, host="0.0.0.0", port=config.PORT)
