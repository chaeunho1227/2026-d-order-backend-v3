package com.example.spring.dto.redis;

import com.fasterxml.jackson.annotation.JsonProperty;
import lombok.AccessLevel;
import lombok.Getter;
import lombok.NoArgsConstructor;

import java.time.OffsetDateTime;

@Getter
@NoArgsConstructor(access = AccessLevel.PROTECTED)
public class OrderCancelledMessageDto {

    @JsonProperty("event_id")
    private String eventId;

    @JsonProperty("event_type")
    private String eventType;

    @JsonProperty("occurred_at")
    private OffsetDateTime occurredAt;

    /**
     * Envelope payload: { ..., "data": { ... } }
     */
    @JsonProperty("data")
    private Data data;

    /**
     * Flat payload compatibility: { "order_item_id": ... }
     */
    @JsonProperty("table_usage_id")
    private Long tableUsageId;

    @JsonProperty("order_id")
    private Long orderId;

    @JsonProperty("order_item_id")
    private Long orderItemId;

    @JsonProperty("menu_name")
    private String menuName;

    @JsonProperty("cancel_quantity")
    private Integer cancelQuantity;

    @JsonProperty("refund_price")
    private Integer refundPrice;

    @JsonProperty("is_full_cancel")
    private Boolean isFullCancel;

    @JsonProperty("pushed_at")
    private OffsetDateTime pushedAt;

    public Long getResolvedOrderItemId() {
        if (data != null && data.getOrderItemId() != null) {
            return data.getOrderItemId();
        }
        return orderItemId;
    }

    public Integer getResolvedCancelQuantity() {
        if (data != null && data.getCancelQuantity() != null) {
            return data.getCancelQuantity();
        }
        return cancelQuantity;
    }

    public Boolean getResolvedIsFullCancel() {
        if (data != null && data.getIsFullCancel() != null) {
            return data.getIsFullCancel();
        }
        return isFullCancel;
    }

    @Getter
    @NoArgsConstructor(access = AccessLevel.PROTECTED)
    public static class Data {

        @JsonProperty("table_usage_id")
        private Long tableUsageId;

        @JsonProperty("order_id")
        private Long orderId;

        @JsonProperty("order_item_id")
        private Long orderItemId;

        @JsonProperty("menu_name")
        private String menuName;

        @JsonProperty("cancel_quantity")
        private Integer cancelQuantity;

        @JsonProperty("refund_price")
        private Integer refundPrice;

        @JsonProperty("is_full_cancel")
        private Boolean isFullCancel;

        @JsonProperty("pushed_at")
        private OffsetDateTime pushedAt;
    }
}