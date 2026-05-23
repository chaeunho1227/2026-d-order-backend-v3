import logging
from http.cookies import SimpleCookie

from channels.middleware import BaseMiddleware
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken

logger = logging.getLogger(__name__)


class JWTCookieMiddleware:
    """
    HTTP 요청용.
    쿠키의 access_token을 Authorization 헤더로 변환.
    프론트에서 credentials: 'include'로 보내면
    DRF JWTAuthentication이 인식할 수 있게 해줌.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        access_token = request.COOKIES.get('access_token')
        if access_token and 'HTTP_AUTHORIZATION' not in request.META:
            request.META['HTTP_AUTHORIZATION'] = f'Bearer {access_token}'
        return self.get_response(request)


class StaleCookiePurgeMiddleware:
    """모든 HTTP 응답에 옛 도메인 잔재 쿠키 expire를 자동 부착한다.

    과거 `Domain=.dorder-api.shop` 등 부모 도메인으로 발급된 쿠키가
    브라우저에 남아있으면 host-only 쿠키와 동시 전송되어 CSRF 등 인증 실패
    원인이 된다. login/refresh/csrf-token/logout endpoint에만 정리를 부착하면
    해당 endpoint에 도달조차 못하는 사용자(예: 첫 진입 GET에서 403)는
    복구 기회가 없으므로, 응답 전반에 자동 부착.

    클라이언트가 한 번 정리되면 STALE_PURGE_MARKER 쿠키가 set되고,
    이후 요청은 cleanup을 건너뛴다.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        from authentication.utils import clear_stale_domain_cookies
        clear_stale_domain_cookies(response, request)
        return response


@sync_to_async(thread_sensitive=True)
def get_user_from_token(token_str):
    try:
        User = get_user_model()
        token = AccessToken(token_str)
        user = User.objects.get(id=token['user_id'])
        session_id = token.get('session_id')
        logger.debug("[JWTWebSocketMiddleware] Token OK → user=%s, session_id=%s", user, session_id)
        return user, session_id
    except Exception as e:
        logger.error("[JWTWebSocketMiddleware] Invalid token: %s", e, exc_info=True)
        return None, None


# 고객용(인증 불필요) WebSocket endpoint prefix.
# 여기 매칭되면 access_token 부재 시 경고 로깅 생략 (정상 흐름).
PUBLIC_WS_PATH_PREFIXES = (
    "/ws/django/cart/",
)


class JWTWebSocketMiddleware(BaseMiddleware):
    """
    WebSocket 연결용.
    핸드셰이크 시 전송되는 쿠키에서 access_token을 추출하여
    scope["user"]를 설정.
    """
    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        cookie_header = headers.get(b"cookie", b"").decode()

        cookies = SimpleCookie(cookie_header)
        access_token = cookies.get("access_token")

        scope["user"] = AnonymousUser()

        if access_token:
            user, session_id = await get_user_from_token(access_token.value)
            if user:
                scope["user"] = user
                scope["session_id"] = session_id
                logger.info("[JWTWebSocketMiddleware] Token OK → user set: %s, session_id=%s", user, session_id)
            else:
                logger.warning("[JWTWebSocketMiddleware] Token provided but no valid user found")
        else:
            path = scope.get("path", "")
            is_public = any(path.startswith(p) for p in PUBLIC_WS_PATH_PREFIXES)
            if not is_public:
                logger.warning("[JWTWebSocketMiddleware] No token in cookie | path=%s", path)

        return await super().__call__(scope, receive, send)
