package com.example.spring.config;

import org.springframework.http.*;
import org.springframework.web.client.RestTemplate;
import lombok.extern.slf4j.Slf4j;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

@Slf4j
public class DjangoApiUtil {
    /**
     * CSRF 토큰 발급 (Django API 호출)
     * @param restTemplate RestTemplate 인스턴스
     * @param djangoApiBaseUrl Django API base url
     * @return CSRF 토큰 정보 (csrfToken, csrfCookie)
     */
    public static Map<String, String> getCsrfToken(RestTemplate restTemplate, String djangoApiBaseUrl) {
        String url = djangoApiBaseUrl + "/api/v3/django/auth/csrf-token/";
        try {
            ResponseEntity<Map> response = restTemplate.exchange(url, HttpMethod.GET, null, Map.class);
            Map<String, String> result = new HashMap<>();
            Map body = response.getBody();
            if (body != null && body.get("csrfToken") != null) {
                result.put("csrfToken", body.get("csrfToken").toString());
            }
            // Django 응답에는 진짜 csrftoken Set-Cookie 외에 StaleCookiePurgeMiddleware 가
            // 부착하는 delete-cookie 라인 (`csrftoken=; Domain=...; expires=Thu, 01-Jan-1970...`)
            // 도 함께 와서, 단순히 startsWith("csrftoken=") + 덮어쓰기 패턴은 마지막에
            // 빈 값으로 덮어써질 위험이 있다. key 매칭 + value 비어있지 않음 체크 필요.
            List<String> cookies = response.getHeaders().get(HttpHeaders.SET_COOKIE);
            if (cookies != null) {
                for (String cookie : cookies) {
                    String[] parts = cookie.split(";", 2);
                    String[] kv = parts[0].split("=", 2);
                    if (kv.length == 2
                            && "csrftoken".equals(kv[0].trim())
                            && !kv[1].isEmpty()) {
                        result.put("csrfCookie", parts[0]);
                    }
                }
            }
            return result;
        } catch (Exception e) {
            log.error("CSRF 토큰 발급 실패: {}", e.getMessage());
            return new HashMap<>();
        }
    }
}
