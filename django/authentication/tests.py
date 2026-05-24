from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth.models import User

from booth.models import Booth
from core.test_utils import IN_MEMORY_STORAGES, suppress_request_warnings


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class SignupViewTest(APITestCase):
    """회원가입 API 테스트"""

    def setUp(self):
        self.signup_url = '/api/v3/django/auth/signup/'
        self.valid_data = {
            "username": "testuser",
            "password": "testpass123",
            "booth_data": {
                "name": "테스트 부스",
                "table_max_cnt": 10,
                "account": "1234567890",
                "depositor": "홍길동",
                "bank": "신한은행",
                "seat_type": "NO",
                "seat_fee_person": 0,
                "seat_fee_table": 0,
                "table_limit_hours": 2.0
            }
        }

    def test_signup_success(self):
        """회원가입 성공 테스트"""
        response = self.client.post(
            self.signup_url,
            self.valid_data,
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(username='testuser').exists())
        self.assertTrue(Booth.objects.filter(name='테스트 부스').exists())

    def test_signup_sets_cookies(self):
        """회원가입 시 JWT 쿠키 설정 테스트"""
        response = self.client.post(
            self.signup_url,
            self.valid_data,
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn('access_token', response.cookies)
        self.assertIn('refresh_token', response.cookies)

    def test_signup_duplicate_username(self):
        """중복 username 회원가입 실패 테스트"""
        User.objects.create_user(username='testuser', password='existingpass')

        with suppress_request_warnings():
            response = self.client.post(
                self.signup_url,
                self.valid_data,
                format='json'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_missing_username(self):
        """username 누락 시 실패 테스트"""
        invalid_data = self.valid_data.copy()
        del invalid_data['username']

        with suppress_request_warnings():
            response = self.client.post(
                self.signup_url,
                invalid_data,
                format='json'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_missing_password(self):
        """password 누락 시 실패 테스트"""
        invalid_data = self.valid_data.copy()
        del invalid_data['password']

        with suppress_request_warnings():
            response = self.client.post(
                self.signup_url,
                invalid_data,
                format='json'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_missing_booth_data(self):
        """booth_data 누락 시 실패 테스트"""
        invalid_data = {
            'username': 'testuser',
            'password': 'testpass123'
        }

        with suppress_request_warnings():
            response = self.client.post(
                self.signup_url,
                invalid_data,
                format='json'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signup_creates_booth_with_user(self):
        """회원가입 시 User와 Booth가 연결되는지 테스트"""
        response = self.client.post(
            self.signup_url,
            self.valid_data,
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        user = User.objects.get(username='testuser')
        booth = Booth.objects.get(user=user)

        self.assertEqual(booth.name, '테스트 부스')
        self.assertEqual(booth.table_max_cnt, 10)
        self.assertEqual(booth.bank, '신한은행')

class CheckUsernameViewTest(APITestCase):
    def setUp(self):
        self.username_check_url = '/api/v3/django/auth/check-username/'
        User.objects.create_user(username='existinguser', password='testpass')

    def test_username_parameter_missing(self):
        """username 파라미터 누락 시 실패 테스트"""
        with suppress_request_warnings():
            response = self.client.get(self.username_check_url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    
    def test_username_available(self):
        """사용 가능한 username 체크 테스트"""
        response = self.client.get(
            self.username_check_url,
            {'username': 'newuser'}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['is_available'])

    def test_username_unavailable(self):
        """사용 불가능한 username 체크 테스트"""
        response = self.client.get(
            self.username_check_url,
            {'username': 'existinguser'}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['data']['is_available'])


class AuthApiViewTest(APITestCase):
    def setUp(self):
        self.auth_url = '/api/v3/django/auth/'
        self.username = 'testuser'
        self.password = 'testpass123'
        User.objects.create_user(username=self.username, password=self.password)

    def test_login_success(self):
        """로그인 성공 테스트"""
        response = self.client.post(
            self.auth_url,
            {
                'username': self.username,
                'password': self.password
            },
            format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access_token', response.cookies)
        self.assertIn('refresh_token', response.cookies)    

    def test_login_invalid_username(self):
        """잘못된 username 로그인 실패 테스트"""
        with suppress_request_warnings():
            response = self.client.post(
                self.auth_url,
                {
                    'username': 'wronguser',
                    'password': self.password
                },
                format='json'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_invalid_password(self):
        """잘못된 password 로그인 실패 테스트"""
        with suppress_request_warnings():
            response = self.client.post(
                self.auth_url,
                {
                    'username': self.username,
                    'password': 'wrongpass'
                },
                format='json'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
    
    def test_token_verify_success(self):
        """토큰 유효성 확인 성공 테스트"""
        # 먼저 로그인하여 토큰 획득
        login_response = self.client.post(
            self.auth_url,
            {
                'username': self.username,
                'password': self.password
            },
            format='json'
        )

        access_token = login_response.cookies.get('access_token').value

        # 토큰 유효성 확인 요청 (POST /api/v3/auth/refresh/)
        response = self.client.post(
            '/api/v3/django/auth/refresh/',
            HTTP_COOKIE=f'access_token={access_token}'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['username'], self.username)

    def test_token_verify_missing_token(self):
        """토큰 없이 유효성 확인 시 실패 테스트"""
        with suppress_request_warnings():
            response = self.client.post('/api/v3/django/auth/refresh/')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
    
    def test_logout_success(self):
        """로그아웃 성공 테스트"""
        # 먼저 로그인하여 토큰 획득
        login_response = self.client.post(
            self.auth_url,
            {
                'username': self.username,
                'password': self.password
            },
            format='json'
        )

        response = self.client.delete(
            self.auth_url,
            HTTP_COOKIE=(
                f"access_token={login_response.cookies.get('access_token').value}; "
                f"refresh_token={login_response.cookies.get('refresh_token').value}"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # delete_cookie는 값을 비우고 max-age=0으로 설정하여 브라우저가 삭제하도록 함
        self.assertEqual(response.cookies['access_token'].value, '')
        self.assertEqual(response.cookies['access_token']['max-age'], 0)
        self.assertEqual(response.cookies['refresh_token'].value, '')
        self.assertEqual(response.cookies['refresh_token']['max-age'], 0)
    

class CsrfTokenViewTest(APITestCase):

    def setUp(self):
        self.csrf_url = '/api/v3/django/auth/csrf-token/'

    def test_get_csrf_token(self):
        """CSRF 토큰 획득 테스트"""
        response = self.client.get(self.csrf_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('csrfToken', response.data)


class StaleCookiePurgeMiddlewareTest(APITestCase):
    """옛 도메인 잔재 쿠키 자동 일소 미들웨어 테스트.

    부모 도메인(`.dorder-api.shop`)으로 박힌 잔재 쿠키를 응답 전반에서
    자동으로 expire 처리하여 dev/prod 간 CSRF 충돌을 막는다.
    """

    def setUp(self):
        from authentication.utils import _STALE_COOKIE_DOMAINS, _STALE_COOKIE_NAMES, STALE_PURGE_MARKER
        self.stale_domains = _STALE_COOKIE_DOMAINS
        self.stale_names = _STALE_COOKIE_NAMES
        self.marker_name = STALE_PURGE_MARKER
        # 어떤 GET endpoint든 응답 전반에 미들웨어가 작동해야 함.
        # /csrf-token/ 은 STALE_CLEANUP_SKIP_PATHS 에 들어있어 별도 테스트에서 검증.
        self.any_url = '/health/'
        self.skipped_url = '/api/v3/django/auth/csrf-token/'

    def test_attaches_expire_morsel_for_each_domain_name_pair(self):
        """마커가 없는 요청 응답에 (도메인 × 이름) 조합 expire morsel이 모두 부착된다."""
        response = self.client.get(self.any_url)

        for domain in self.stale_domains:
            for name in self.stale_names:
                key = f'__stale__{name}__{domain}'
                self.assertIn(key, response.cookies)
                morsel = response.cookies[key]
                self.assertEqual(morsel.key, name)
                self.assertEqual(morsel.value, '')
                self.assertEqual(morsel['max-age'], 0)
                self.assertEqual(morsel['domain'], domain)
                self.assertEqual(morsel['path'], '/')

    def test_sets_purge_marker(self):
        """정리가 일어난 응답에는 마커 쿠키가 host-only로 set된다."""
        response = self.client.get(self.any_url)

        self.assertIn(self.marker_name, response.cookies)
        marker = response.cookies[self.marker_name]
        self.assertEqual(marker.value, '1')
        # host-only: domain 비어있어야 함
        self.assertEqual(marker['domain'], '')

    def test_skips_when_marker_already_set(self):
        """이미 마커 쿠키를 보유한 요청은 cleanup을 skip한다."""
        response = self.client.get(
            self.any_url,
            HTTP_COOKIE=f'{self.marker_name}=1',
        )

        # expire morsel이 응답에 부착되지 않아야 함
        for domain in self.stale_domains:
            for name in self.stale_names:
                self.assertNotIn(
                    f'__stale__{name}__{domain}',
                    response.cookies,
                )
        # 마커도 다시 set되지 않아야 함 (이미 있으니까)
        self.assertNotIn(self.marker_name, response.cookies)

    def test_skips_for_csrf_token_endpoint(self):
        """/csrf-token/ 응답에는 stale cleanup 모르셀이 부착되지 않는다.

        단순 cookie 파서를 쓰는 클라이언트 (Spring DjangoApiUtil 등) 가
        진짜 csrftoken 대신 stale delete 라인의 empty 값을 읽어가는 회귀를
        방지하기 위해 이 경로는 skip.
        """
        response = self.client.get(self.skipped_url)

        # csrftoken 자체는 ensure_csrf_cookie 로 정상 set 되어야 함
        self.assertIn('csrftoken', response.cookies)
        self.assertNotEqual(response.cookies['csrftoken'].value, '')

        # stale morsel 은 부착되지 않아야 함
        for domain in self.stale_domains:
            for name in self.stale_names:
                self.assertNotIn(
                    f'__stale__{name}__{domain}',
                    response.cookies,
                )
        # 마커도 set 되지 않아야 함 (cleanup 자체가 skip)
        self.assertNotIn(self.marker_name, response.cookies)
