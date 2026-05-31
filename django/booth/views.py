from django.db.models import Count, Q

from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.views import APIView
from rest_framework import status
from rest_framework.response import Response
from booth.services import BoothService, BoothStatisticsService
from booth.models import Booth
from booth.serializers import BoothSerializer, BoothUpdateSerializer

# Create your views here.
class BoothMyPageAPIView(APIView):
    """부스 마이페이지 열람 / 정보 수정 API"""
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """마이페이지 열람"""
        user = request.user
        booth = user.booth

        serializer = BoothSerializer(booth)

        return Response({
            "message": "부스 데이터를 불러왔습니다.",
            "data" : serializer.data
        }, status=status.HTTP_200_OK)

    def patch(self, request):
        """마이페이지 정보 수정"""
        user = request.user
        booth = user.booth

        serializer = BoothUpdateSerializer(booth, data=request.data, partial = True)
        
        # 유효성 검증
        serializer.is_valid(raise_exception=True)

        BoothService.update_booth(booth, serializer.validated_data)

        return Response({
            "message": "업데이트가 완료되었습니다.",
            "data" : serializer.data
        }, status=status.HTTP_200_OK)

class BoothMyPageQRcodeAPIView(APIView):
    """부스 QR URL"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """QR URL 반환"""
        booth = request.user.booth

        return Response({
            "message": "QR URL을 조회하였습니다.",
            "data" : {
                "qr_image_url" : booth.qr_image.url,
            }
        }, status=status.HTTP_200_OK)

class BoothTableUsageResetAPIView(APIView):
    """테스트 데이터를 지우기 위한 초기화
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        booth = request.user.booth
        deleted_count = BoothService.reset_booth_table_usage(booth)
        return Response({
            'message': '모든 테이블 사용 기록이 삭제되었습니다.',
            'data': {'deleted_count': deleted_count},
        }, status=status.HTTP_200_OK)


class BoothNamePublicAPIView(APIView):
    """부스 이름 조회용"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, booth_uuid):

        # 못찾을때
        try:
            booth = Booth.objects.get(public_id=booth_uuid)
        except Booth.DoesNotExist:
            return Response({
                "message" : "해당 부스를 찾을 수 없습니다."
            }, status=status.HTTP_404_NOT_FOUND)
    

        return Response({
            "message": "부스 이름을 조회하였습니다.",
            "data" : {
                "booth_name" : booth.name,
            }
        }, status=status.HTTP_200_OK)
        
class BoothNameAPIView(APIView):
    """부스 이름 조회용"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """QR URL 반환"""
        booth = request.user.booth

        return Response({
            "message": "부스 이름을 조회하였습니다.",
            "data" : {
                "booth_name" : booth.name,
            }
        }, status=status.HTTP_200_OK)


class BoothStatisticsAPIView(APIView):
    """부스 통계 조회 API (본인 부스)"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        booth = request.user.booth
        data = BoothStatisticsService.get_statistics(booth, request=request)
        return Response({
            "message": "통계 데이터를 불러왔습니다.",
            "data": data,
        }, status=status.HTTP_200_OK)


class BoothStatisticsAllAPIView(APIView):
    """전체 부스 통계 조회 API (인증 불필요)"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        booths = Booth.objects.all().order_by('pk')
        result = []
        for booth in booths:
            stats = BoothStatisticsService.get_statistics(booth, request=request)
            result.append({
                "booth_id": booth.pk,
                "booth_uuid": str(booth.public_id),
                "booth_name": booth.name,
                **stats,
            })
        return Response({
            "message": "전체 부스 통계 데이터를 불러왔습니다.",
            "data": result,
        }, status=status.HTTP_200_OK)


class BoothStatisticsPublicAPIView(APIView):
    """부스별 통계 조회 API (UUID, 인증 불필요)"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, booth_uuid):
        try:
            booth = Booth.objects.get(public_id=booth_uuid)
        except Booth.DoesNotExist:
            return Response(
                {"message": "해당 부스를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        data = BoothStatisticsService.get_statistics(booth, request=request)
        return Response({
            "message": "통계 데이터를 불러왔습니다.",
            "data": {
                "booth_id": booth.pk,
                "booth_name": booth.name,
                **data,
            },
        }, status=status.HTTP_200_OK)


class BoothAdBannerAPIView(APIView):
    """광고 배너용 부스 목록 조회 API (인증 불필요)"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        date_str = request.query_params.get('date')
        if not date_str:
            return Response(
                {"message": "date 파라미터가 필요합니다. (예: ?date=2026-05-23)"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        booths = (
            Booth.objects
            .filter(location__has_key=date_str)
            .annotate(
                total_table=Count('tables'),
                remaining_table=Count('tables', filter=Q(tables__status='AVAILABLE')),
            )
            .order_by('pk')
        )

        booth_details = [
            {
                "boothName": booth.host_name,
                "location": booth.location.get(date_str, ""),
                "totalTable": booth.total_table,
                "remainingTable": booth.remaining_table,
                "thumbnailUrl": booth.thumbnail_image.url if booth.thumbnail_image else None,
            }
            for booth in booths
            if booth.location.get(date_str)
        ]

        return Response({
            "message": "부스 광고 배너 정보 조회 성공",
            "data": booth_details,
        }, status=status.HTTP_200_OK)