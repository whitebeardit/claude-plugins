"""Bill classification rules (D3): same categories for every company, EC2-Other split by usage type."""
import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ce_breakdown.py"
spec = importlib.util.spec_from_file_location("ce_breakdown", SCRIPT)
ce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ce)


class ClassifyService(unittest.TestCase):
    def test_compute(self):
        for name in ["Amazon Elastic Compute Cloud - Compute", "AWS Lambda", "Amazon Elastic Container Service",
                     "Amazon Elastic Container Service for Kubernetes", "Amazon API Gateway"]:
            self.assertEqual(ce.classify_service(name), ("Compute", True), name)

    def test_data_and_storage(self):
        self.assertEqual(ce.classify_service("Amazon Relational Database Service"), ("Data & databases", False))
        self.assertEqual(ce.classify_service("Amazon DynamoDB"), ("Data & databases", False))
        self.assertEqual(ce.classify_service("Amazon Simple Storage Service"), ("Storage", True))
        self.assertEqual(ce.classify_service("Amazon EC2 Container Registry (ECR)"), ("Storage", True))

    def test_network_observability_security_tax(self):
        self.assertEqual(ce.classify_service("Amazon Elastic Load Balancing"), ("Network", True))
        self.assertEqual(ce.classify_service("Amazon CloudFront"), ("Network", True))
        self.assertEqual(ce.classify_service("AmazonCloudWatch"), ("Observability", True))
        self.assertEqual(ce.classify_service("AWS Key Management Service"), ("Security & identity", False))
        self.assertEqual(ce.classify_service("Tax"), ("Tax", False))

    def test_commitments_win_over_compute_keywords(self):
        self.assertEqual(ce.classify_service("Savings Plans for AWS Compute usage"), ("Commitments (SP/RI)", False))

    def test_marketplace_set_wins_over_keywords(self):
        vendor = "Apache Kafka on Confluent Cloud - Pay As You Go"
        self.assertEqual(ce.classify_service(vendor, marketplace={vendor}), ("SaaS & Marketplace", False))
        self.assertEqual(ce.classify_service("Redis Cloud - Annual", marketplace={"Redis Cloud - Annual"}), ("SaaS & Marketplace", False))

    def test_unknown_goes_to_other(self):
        self.assertEqual(ce.classify_service("Some New Service"), ("Other", False))


class ClassifyEc2Other(unittest.TestCase):
    def test_storage(self):
        self.assertEqual(ce.classify_ec2_other("EBS:VolumeUsage.gp3"), ("Storage", True))
        self.assertEqual(ce.classify_ec2_other("USE1-EBS:SnapshotUsage"), ("Storage", True))

    def test_network(self):
        for ut in ["DataTransfer-Regional-Bytes", "NatGateway-Hours", "NatGateway-Bytes", "PublicIPv4:InUseAddress", "USE1-USW2-AWS-Out-Bytes", "ElasticIP:IdleAddress"]:
            self.assertEqual(ce.classify_ec2_other(ut), ("Network", True), ut)

    def test_compute_credits(self):
        self.assertEqual(ce.classify_ec2_other("CPUCredits:t3"), ("Compute", True))

    def test_unknown(self):
        self.assertEqual(ce.classify_ec2_other("SomethingElse"), ("Other", False))


class CategoryVocabulary(unittest.TestCase):
    def test_every_rule_category_is_in_the_fixed_list(self):
        fixed = {"Compute", "Network", "Data & databases", "Storage", "SaaS & Marketplace", "Commitments (SP/RI)",
                 "Observability", "Security & identity", "Tax", "Other"}
        for category, _, _ in ce.CATEGORY_RULES:
            self.assertIn(category, fixed)


if __name__ == "__main__":
    unittest.main()
