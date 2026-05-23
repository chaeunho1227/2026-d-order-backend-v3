"""
Gunicorn 커스텀 logger.

access log 에서 healthcheck 요청을 제외하여 CloudWatch ingestion 및 가독성 개선.
Docker 컨테이너 healthcheck (15초 간격) + nginx → /health/django proxy 등으로
분당 다수 호출되는데 비즈니스 가치가 없음.

활성화: gunicorn 명령에 --logger-class project.gunicorn_logger.HealthFilteringLogger
"""
from gunicorn.glogging import Logger as GunicornLogger


class HealthFilteringLogger(GunicornLogger):
    """access log 에서 /health, /health/ 요청 제외."""

    HEALTH_PATHS = frozenset(["/health", "/health/"])

    def access(self, resp, req, environ, request_time):
        if environ.get("PATH_INFO") in self.HEALTH_PATHS:
            return
        super().access(resp, req, environ, request_time)
