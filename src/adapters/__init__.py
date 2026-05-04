"""Adapters package — explicit registration on import.

Importing `src.adapters` (or anything from it) wires every adapter
into the registry. Add new adapter modules below.
"""
from src.adapters.aws_db_instance import AwsDbInstanceAdapter
from src.adapters.aws_instance import AwsInstanceAdapter
from src.adapters.aws_lambda_function import AwsLambdaFunctionAdapter
from src.adapters.aws_nat_gateway import AwsNatGatewayAdapter
from src.adapters.aws_s3_bucket import AwsS3BucketAdapter
from src.adapters.google_cloud_run_v2_service import (
    GoogleCloudRunV2ServiceAdapter,
)
from src.adapters.google_compute_instance import GoogleComputeInstanceAdapter
from src.adapters.google_sql_database_instance import (
    GoogleSqlDatabaseInstanceAdapter,
)
from src.adapters.registry import (
    get_adapter,
    register_adapter,
    registered_types,
)

register_adapter(AwsInstanceAdapter())
register_adapter(AwsNatGatewayAdapter())
register_adapter(AwsDbInstanceAdapter())
register_adapter(AwsLambdaFunctionAdapter())
register_adapter(AwsS3BucketAdapter())
register_adapter(GoogleComputeInstanceAdapter())
register_adapter(GoogleSqlDatabaseInstanceAdapter())
register_adapter(GoogleCloudRunV2ServiceAdapter())

__all__ = ["get_adapter", "register_adapter", "registered_types"]
