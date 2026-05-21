"""
JWT 쿠키 유틸리티 함수
"""
import http.cookies

from django.conf import settings


# 과거 `Domain=.dorder-api.shop` 부모 도메인으로 발급되어 dev/prod 서브도메인
# 모두로 누수되는 쿠키만 정리 대상. `dev.*`/`prod.*`/`admin.*`는 host-only라
# 다른 환경으로 누수되지 않아 정리할 필요가 없고, 변형을 추가할수록 응답 헤더가
# 부풀어 nginx `proxy_buffer_size` 초과로 502를 유발한다 (#440 hotfix).
_STALE_COOKIE_DOMAINS = (
    '.dorder-api.shop',
    'dorder-api.shop',
)
_STALE_COOKIE_NAMES = ('csrftoken', 'sessionid', 'access_token', 'refresh_token')

# 정리 1회 실행 식별자. 정리 대상이 바뀌면 v3/v4로 bump하여 기존 클라이언트 재정리를 유도.
# v1: 8 도메인 × 4 이름 = 32 헤더 → 응답 헤더 too big으로 502 유발
# v2: 2 부모 도메인 × 4 이름 = 8 헤더 (현재)
STALE_PURGE_MARKER = '_stale_purged_v2'


def clear_stale_domain_cookies(response, request=None):
    """옛 도메인 쿠키(부모도메인/서브도메인 변형)를 모두 expire 처리한다.

    Django response.cookies는 SimpleCookie(이름 기준 dict)라 같은 이름으로
    여러 도메인 변형을 delete_cookie 호출하면 마지막 entry만 살아남고,
    또한 후속 set_cookie(domain=None) 호출 시 기존 domain 속성이 누수되어
    새 쿠키에 잘못된 Domain attr이 붙는 문제가 있다.
    이를 회피하기 위해 각 변형을 고유한 dict 키로 Morsel 객체를 직접 넣는다.
    Morsel.key는 'csrftoken' 등 원본 이름을 유지하므로 Set-Cookie 출력은 정상.

    마커 쿠키(STALE_PURGE_MARKER)가 이미 있는 요청은 한 번 정리된 것으로 보고 skip.
    """
    if request is not None and request.COOKIES.get(STALE_PURGE_MARKER) == '1':
        return response

    past = 'Thu, 01-Jan-1970 00:00:00 GMT'
    for domain in _STALE_COOKIE_DOMAINS:
        for name in _STALE_COOKIE_NAMES:
            morsel = http.cookies.Morsel()
            morsel.set(name, '', '')
            morsel['domain'] = domain
            morsel['expires'] = past
            morsel['max-age'] = 0
            morsel['path'] = '/'
            response.cookies[f'__stale__{name}__{domain}'] = morsel

    response.set_cookie(
        STALE_PURGE_MARKER,
        '1',
        max_age=60 * 60 * 24 * 365,
        samesite='Lax',
        secure=not settings.IS_LOCAL,
        httponly=True,
    )
    return response


def set_jwt_cookies(response, access_token, refresh_token):
    """
    Response 객체에 JWT 쿠키를 설정합니다.

    Args:
        response: Django Response 객체
        access_token: JWT access token 문자열
        refresh_token: JWT refresh token 문자열

    Returns:
        response: 쿠키가 설정된 Response 객체
    """
    jwt_settings = settings.SIMPLE_JWT
    domain = jwt_settings.get('AUTH_COOKIE_DOMAIN')

    # Access Token 쿠키 설정
    response.set_cookie(
        key=jwt_settings.get('AUTH_COOKIE'),
        value=access_token,
        max_age=int(jwt_settings.get('ACCESS_TOKEN_LIFETIME').total_seconds()),
        httponly=jwt_settings.get('AUTH_COOKIE_HTTP_ONLY'),
        samesite=jwt_settings.get('AUTH_COOKIE_SAMESITE'),
        secure=jwt_settings.get('AUTH_COOKIE_SECURE'),
        domain=domain,
    )

    # Refresh Token 쿠키 설정
    response.set_cookie(
        key=jwt_settings.get('AUTH_COOKIE_REFRESH'),
        value=refresh_token,
        max_age=int(jwt_settings.get('REFRESH_TOKEN_LIFETIME').total_seconds()),
        httponly=jwt_settings.get('AUTH_COOKIE_HTTP_ONLY'),
        samesite=jwt_settings.get('AUTH_COOKIE_SAMESITE'),
        secure=jwt_settings.get('AUTH_COOKIE_SECURE'),
        domain=domain,
    )

    return response


def delete_jwt_cookies(response):
    """
    Response 객체에서 JWT 쿠키를 삭제합니다.

    Args:
        response: Django Response 객체

    Returns:
        response: 쿠키가 삭제된 Response 객체
    """
    jwt_settings = settings.SIMPLE_JWT

    samesite = jwt_settings.get('AUTH_COOKIE_SAMESITE')
    domain = jwt_settings.get('AUTH_COOKIE_DOMAIN')

    response.delete_cookie(jwt_settings.get('AUTH_COOKIE'), samesite=samesite, domain=domain)
    response.delete_cookie(jwt_settings.get('AUTH_COOKIE_REFRESH'), samesite=samesite, domain=domain)

    return response
