package com.example.spring.service.serving;

import com.example.spring.dto.redis.OrderCancelledMessageDto;
import com.example.spring.dto.redis.OrderCookedMessageDto;
import com.example.spring.event.RedisMessageEvent;
import com.example.spring.service.staffcall.StaffCallTableResetService;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.UUID;

@Slf4j
@Component
@RequiredArgsConstructor
public class ServingTaskEventListener {

    private final ServingTaskService servingTaskService;
    private final StaffCallTableResetService staffCallTableResetService;
    private final ObjectMapper objectMapper;

    @EventListener
    @Transactional
    public void handleRedisMessage(RedisMessageEvent event) {
        String channel = event.getChannel();
        String message = event.getMessage();

        if (!(channel.startsWith("django:booth:") && channel.contains(":order:"))) {
            return;
        }

        try {
            Long boothId = extractBoothId(channel);
            String orderEvent = extractOrderEvent(channel);

            /*
             * 주문 취소 이벤트는 payload 구조가 cooked/served/cooking/reset과 다를 수 있습니다.
             * 예: { event_id, event_type, occurred_at, data: { order_item_id, ... } }
             * 따라서 OrderCookedMessageDto로 먼저 파싱하지 않고, 전용 DTO로 먼저 처리합니다.
             */
            if ("cancelled".equals(orderEvent) || "refund".equals(orderEvent)) {
                handleCancelledEvent(boothId, channel, message, orderEvent);
                return;
            }

            /*
             * 기존 serving lifecycle 이벤트는 기존 DTO 사용
             * - cooked
             * - served
             * - cooking
             * - reset
             */
            OrderCookedMessageDto dto = objectMapper.readValue(message, OrderCookedMessageDto.class);

            if ("cooked".equals(orderEvent)) {
                if (dto.getOrderItemId() == null) {
                    log.warn("[cooked 처리 스킵] order_item_id 누락. channel={}, message={}", channel, message);
                    return;
                }

                if (dto.getTableNum() == null) {
                    log.warn(
                            "[cooked 처리 경고] table_num/table_number 누락. 생성은 진행하지만 reset-by-table 정리에 실패할 수 있습니다. channel={}, message={}",
                            channel,
                            message
                    );
                }

                if (dto.getMenuName() == null || dto.getMenuName().isBlank()) {
                    log.warn(
                            "[cooked 처리 경고] menu_name 누락. 프론트 표시 데이터가 비어있을 수 있습니다. channel={}, message={}",
                            channel,
                            message
                    );
                }

                if (dto.getQuantity() == null) {
                    log.warn(
                            "[cooked 처리 경고] quantity 누락. 프론트 표시 데이터가 비어있을 수 있습니다. channel={}, message={}",
                            channel,
                            message
                    );
                }

                servingTaskService.createNewServingTask(
                        boothId,
                        dto.getOrderItemId(),
                        dto.getTableNum(),
                        dto.getMenuId(),
                        dto.getMenuName(),
                        dto.getQuantity(),
                        UUID.randomUUID().toString()
                );

                log.info("[서빙 태스크 생성 완료] boothId={}, orderItemId={}", boothId, dto.getOrderItemId());
                return;
            }

            if ("served".equals(orderEvent)) {
                if (dto.getOrderItemId() == null) {
                    log.warn("[served 처리 스킵] order_item_id 누락. channel={}, message={}", channel, message);
                    return;
                }

                servingTaskService.removeTasksByOrderItemId(
                        boothId,
                        dto.getOrderItemId(),
                        "ORDER_SERVED"
                );
                return;
            }

            if ("cooking".equals(orderEvent)) {
                if (dto.getOrderItemId() == null) {
                    log.warn("[cooking 처리 스킵] order_item_id 누락. channel={}, message={}", channel, message);
                    return;
                }

                servingTaskService.removeTasksByOrderItemId(
                        boothId,
                        dto.getOrderItemId(),
                        "COOKING_ROLLBACK"
                );
                return;
            }

            if ("reset".equals(orderEvent)) {
                if (dto.getTableNum() == null) {
                    log.warn("[reset 처리 스킵] table_num/table_number 누락. channel={}, message={}", channel, message);
                    return;
                }

                servingTaskService.removeTasksByTableNumber(
                        boothId,
                        dto.getTableNum(),
                        "TABLE_RESET"
                );

                try {
                    List<com.example.spring.domain.staffcall.StaffCall> cancelled =
                            staffCallTableResetService.voidActiveCallsForTable(boothId, dto.getTableNum());

                    staffCallTableResetService.publishTableResetNotifications(boothId, cancelled);
                } catch (Exception e) {
                    log.error("[reset staffcall 처리 실패] boothId={}, tableNum={}", boothId, dto.getTableNum(), e);
                }

                return;
            }

            log.debug("[Redis order 이벤트 무시] 지원하지 않는 orderEvent입니다. channel={}, orderEvent={}", channel, orderEvent);

        } catch (Exception e) {
            log.error("[Redis 메시지 처리 실패] channel={}, message={}", channel, message, e);
        }
    }

    private void handleCancelledEvent(Long boothId, String channel, String message, String orderEvent) {
        try {
            OrderCancelledMessageDto cancelledDto =
                    objectMapper.readValue(message, OrderCancelledMessageDto.class);

            Long orderItemId = cancelledDto.getResolvedOrderItemId();

            if (orderItemId == null) {
                log.warn("[cancelled/refund 처리 스킵] order_item_id 누락. channel={}, message={}", channel, message);
                return;
            }

            log.info(
                    "[주문 취소 수신] boothId={}, orderItemId={}, isFullCancel={}, cancelQuantity={}, channelSuffix={}",
                    boothId,
                    orderItemId,
                    cancelledDto.getResolvedIsFullCancel(),
                    cancelledDto.getResolvedCancelQuantity(),
                    orderEvent
            );

            servingTaskService.removeTasksByOrderItemId(
                    boothId,
                    orderItemId,
                    "ORDER_CANCELLED"
            );

        } catch (Exception e) {
            log.error("[cancelled/refund 메시지 처리 실패] channel={}, message={}", channel, message, e);
        }
    }

    /**
     * 예: django:booth:3:order:cooked -> 3 추출
     */
    private Long extractBoothId(String channel) {
        String[] parts = channel.split(":");
        if (parts.length < 5) {
            throw new IllegalArgumentException("유효하지 않은 채널 형식입니다: " + channel);
        }

        return Long.valueOf(parts[2]);
    }

    /**
     * 예: django:booth:3:order:cooked -> cooked 추출
     */
    private String extractOrderEvent(String channel) {
        String[] parts = channel.split(":");
        if (parts.length < 5) {
            throw new IllegalArgumentException("유효하지 않은 채널 형식입니다: " + channel);
        }

        return parts[4];
    }
}