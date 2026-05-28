package com.example.spring.controller.serving;

import com.example.spring.domain.serving.ServingTask;
import com.example.spring.dto.serving.response.ServingFilterOptionsData;
import com.example.spring.dto.serving.response.ServingFilterOptionsResponse;
import com.example.spring.dto.serving.response.ServingTaskResponse;
import com.example.spring.security.ServerApiJwtFilter;
import com.example.spring.service.serving.ServingTaskService;
import jakarta.servlet.http.HttpServletRequest;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@Slf4j
@RestController
@RequestMapping("/serving")
@RequiredArgsConstructor
public class ServingTaskController {

    private final ServingTaskService servingTaskService;

    /**
     * 신규 운영자용 API
     * booth_id를 프론트에서 넘기지 않고 JWT 기준으로 조회
     * GET /api/v3/spring/serving/servingcall
     */
    @GetMapping("/servingcall")
    public ResponseEntity<?> getMyPendingCalls(HttpServletRequest request) {
        Long boothId = (Long) request.getAttribute(ServerApiJwtFilter.ATTR_BOOTH_ID);

        if (boothId == null) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        }

        String serverClientId = extractServerClientId(request);
        if (serverClientId == null) {
            return ResponseEntity.badRequest().body("X-Server-Client-Id header is required.");
        }

        List<ServingTask> tasks = servingTaskService.getActiveServingCalls(boothId);
        List<ServingTaskResponse> response = tasks.stream()
                .map(task -> ServingTaskResponse.from(task, serverClientId, true))
                .toList();

        return ResponseEntity.ok(response);
    }

    /**
     * 기존 경로 기반 API는 호환성 유지용
     * 필요 없으면 추후 제거 가능
     */
    @GetMapping("/servingcall/{boothId}")
    public ResponseEntity<?> getPendingCalls(
            @PathVariable Long boothId,
            HttpServletRequest request
    ) {
        Long jwtBooth = (Long) request.getAttribute(ServerApiJwtFilter.ATTR_BOOTH_ID);

        if (jwtBooth == null || !jwtBooth.equals(boothId)) {
            return ResponseEntity.status(HttpStatus.FORBIDDEN).build();
        }

        String serverClientId = extractServerClientId(request);
        if (serverClientId == null) {
            return ResponseEntity.badRequest().body("X-Server-Client-Id header is required.");
        }

        List<ServingTask> tasks = servingTaskService.getActiveServingCalls(boothId);
        List<ServingTaskResponse> response = tasks.stream()
                .map(task -> ServingTaskResponse.from(task, serverClientId, true))
                .toList();

        return ResponseEntity.ok(response);
    }


    @GetMapping("/filter-options")
    public ResponseEntity<?> getFilterOptions(HttpServletRequest request) {
        Long boothId = (Long) request.getAttribute(ServerApiJwtFilter.ATTR_BOOTH_ID);
        String accessToken = (String) request.getAttribute("ACCESS_TOKEN");

        if (boothId == null || accessToken == null || accessToken.isBlank()) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        }

        try {
            ServingFilterOptionsData data = servingTaskService.getFilterOptions(boothId, accessToken);
            ServingFilterOptionsResponse response = ServingFilterOptionsResponse.builder()
                    .message("서빙 필터 옵션 조회 완료")
                    .data(data)
                    .build();
            return ResponseEntity.ok(response);
        } catch (ServingTaskService.DjangoApiException e) {
            return ResponseEntity.status(e.getStatus())
                    .body(Map.of("message", "서빙 필터 옵션 조회 실패", "detail", e.getResponseBody()));
        }
    }

    @PostMapping("/catchcall")
    public ResponseEntity<?> catchCall(
            @RequestParam Long taskId,
            HttpServletRequest httpRequest
    ) {
        Long boothId = (Long) httpRequest.getAttribute(ServerApiJwtFilter.ATTR_BOOTH_ID);

        if (boothId == null) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body("인증 정보가 없습니다.");
        }

        String serverClientId = extractServerClientId(httpRequest);
        if (serverClientId == null) {
            return ResponseEntity.badRequest().body("X-Server-Client-Id header is required.");
        }

        String sessionId = (String) httpRequest.getAttribute(ServerApiJwtFilter.ATTR_SESSION_ID);

        try {
            servingTaskService.catchCall(taskId, boothId, serverClientId, sessionId);
            return ResponseEntity.ok("서빙 요청이 수락되었습니다.");
        } catch (IllegalStateException e) {
            log.warn("[serving catchcall] 상태 충돌 taskId={}, boothId={}: {}", taskId, boothId, e.getMessage());
            return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("message", e.getMessage()));
        } catch (IllegalArgumentException e) {
            log.warn("[serving catchcall] 잘못된 요청 taskId={}, boothId={}: {}", taskId, boothId, e.getMessage());
            return ResponseEntity.badRequest().body(Map.of("message", e.getMessage()));
        }
    }

    @PostMapping("/complete")
    public ResponseEntity<?> completeCall(
            @RequestParam Long taskId,
            HttpServletRequest httpRequest
    ) {
        Long boothId = (Long) httpRequest.getAttribute(ServerApiJwtFilter.ATTR_BOOTH_ID);

        if (boothId == null) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body("인증 정보가 없습니다.");
        }

        String serverClientId = extractServerClientId(httpRequest);
        if (serverClientId == null) {
            return ResponseEntity.badRequest().body("X-Server-Client-Id header is required.");
        }

        try {
            servingTaskService.completeCall(taskId, boothId, serverClientId);
            return ResponseEntity.ok("서빙이 완료되었습니다.");
        } catch (IllegalStateException e) {
            log.warn("[serving complete] 상태 충돌 taskId={}, boothId={}: {}", taskId, boothId, e.getMessage());
            return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("message", e.getMessage()));
        } catch (IllegalArgumentException e) {
            log.warn("[serving complete] 잘못된 요청 taskId={}, boothId={}: {}", taskId, boothId, e.getMessage());
            return ResponseEntity.badRequest().body(Map.of("message", e.getMessage()));
        }
    }

    @PostMapping("/cancel")
    public ResponseEntity<?> cancelCall(
            @RequestParam Long taskId,
            HttpServletRequest httpRequest
    ) {
        Long boothId = (Long) httpRequest.getAttribute(ServerApiJwtFilter.ATTR_BOOTH_ID);

        if (boothId == null) {
            return ResponseEntity.status(HttpStatus.UNAUTHORIZED).body("인증 정보가 없습니다.");
        }

        String serverClientId = extractServerClientId(httpRequest);
        if (serverClientId == null) {
            return ResponseEntity.badRequest().body("X-Server-Client-Id header is required.");
        }

        try {
            servingTaskService.cancelCall(taskId, boothId, serverClientId);
            return ResponseEntity.ok("서빙 수락이 취소되었습니다.");
        } catch (IllegalStateException e) {
            log.warn("[serving cancel] 상태 충돌 taskId={}, boothId={}: {}", taskId, boothId, e.getMessage());
            return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("message", e.getMessage()));
        } catch (IllegalArgumentException e) {
            log.warn("[serving cancel] 잘못된 요청 taskId={}, boothId={}: {}", taskId, boothId, e.getMessage());
            return ResponseEntity.badRequest().body(Map.of("message", e.getMessage()));
        }
    }

    private String extractServerClientId(HttpServletRequest request) {
        String serverClientId = request.getHeader("X-Server-Client-Id");
        if (serverClientId == null || serverClientId.isBlank()) {
            return null;
        }
        return serverClientId;
    }
}