package com.example.spring.dto.serving.response;

import com.example.spring.domain.serving.ServingStatus;
import com.example.spring.domain.serving.ServingTask;
import lombok.Builder;
import lombok.Getter;

import java.time.LocalDateTime;
import java.util.Objects;

@Getter
@Builder
public class ServingTaskResponse {

    private Long taskId;
    private Long orderItemId;
    private Integer tableNumber;
    private Long menuId;
    private String menuName;
    private Integer quantity;
    private String status;
    private String catchedBy;
    private Boolean isMine;
    private Boolean canCatch;
    private Boolean canComplete;
    private Boolean canCancel;
    private LocalDateTime requestedAt;

    public static ServingTaskResponse from(ServingTask task) {
        return from(task, null, false);
    }

    public static ServingTaskResponse from(ServingTask task, String currentUserIdentity, boolean includePermissions) {
        String taskStatus = task.getStatus() != null ? task.getStatus().name() : null;
        boolean isServing = task.getStatus() == ServingStatus.SERVING;
        boolean isRequested = task.getStatus() == ServingStatus.SERVE_REQUESTED;
        Boolean mine = isServing
                ? currentUserIdentity != null && !currentUserIdentity.isBlank() && Objects.equals(task.getCatchedBy(), currentUserIdentity)
                : false;

        return ServingTaskResponse.builder()
                .taskId(task.getId())
                .orderItemId(task.getOrderItemId())
                .tableNumber(task.getTableNumber())
                .menuId(task.getMenuId())
                .menuName(task.getMenuName())
                .quantity(task.getQuantity())
                .status(taskStatus)
                .catchedBy(task.getCatchedBy())
                .isMine(includePermissions ? mine : null)
                .canCatch(includePermissions ? isRequested : null)
                .canComplete(includePermissions ? (isServing && Boolean.TRUE.equals(mine)) : null)
                .canCancel(includePermissions ? (isServing && Boolean.TRUE.equals(mine)) : null)
                .requestedAt(task.getRequestedAt())
                .build();
    }
}