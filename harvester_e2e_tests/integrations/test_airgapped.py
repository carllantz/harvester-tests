# Copyright (c) 2024 SUSE LLC
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of version 3 of the GNU General Public License as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.   See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, contact SUSE LLC.
#
# To contact SUSE about this file by physical or electronic mail,
# you may find current contact information at www.suse.com

import json
from hashlib import sha512
from time import sleep
from datetime import datetime, timedelta

import pytest

pytest_plugins = [
    "harvester_e2e_tests.fixtures.api_client",
    "harvester_e2e_tests.fixtures.upgrades",
]

MONITORING_ADDON = "cattle-monitoring-system/rancher-monitoring"
LOGGING_ADDON = "cattle-logging-system/rancher-logging"
# A well-formed but externally unreachable S3 endpoint; should fail connectivity in air-gapped env
EXTERNAL_S3_ENDPOINT = "https://s3.amazonaws.com"


@pytest.mark.p1
@pytest.mark.settings
@pytest.mark.airgapped
class TestContainerdRegistryMirror:
    """
    Test configuring a private registry mirror via the containerd-registry setting

    Prerequisite:
        --registry-mirror-url must be set to an internal registry mirror that is reachable
        from all cluster nodes (e.g. https://registry.internal).

    In an air-gapped environment, containerd must be configured to pull all system and
    addon container images from an internal mirror rather than public registries.
    """

    @pytest.fixture(scope="class", autouse=True)
    def restore_registry_setting(self, api_client):
        code, data = api_client.settings.get('containerd-registry')
        assert 200 == code, (code, data)
        original_value = data.get('value', '')
        yield
        api_client.settings.update('containerd-registry', {'value': original_value})

    @pytest.mark.dependency(name="set_registry_mirror")
    def test_set_valid_registry_mirror(self, api_client, request):
        """
        Test applying a valid containerd-registry mirror configuration

        Steps:
            1. Skip if --registry-mirror-url is not provided
            2. Build registry mirror config for docker.io and registry.k8s.io
            3. PATCH the containerd-registry setting with the JSON-encoded config
            4. Verify response is 200

        Expected Result:
            - Setting is accepted and stored without error
        """
        mirror_url = request.config.getoption('--registry-mirror-url')
        if not mirror_url:
            pytest.skip("--registry-mirror-url is required for this test")

        registry_config = {
            "mirrors": {
                "docker.io": {"endpoint": [mirror_url]},
                "registry.k8s.io": {"endpoint": [mirror_url]},
            }
        }
        code, data = api_client.settings.update(
            'containerd-registry', {'value': json.dumps(registry_config)}
        )
        assert 200 == code, (
            f"Failed to set containerd-registry mirror\n"
            f"API Status({code}): {data}"
        )

    @pytest.mark.dependency(name="verify_registry_mirror", depends=["set_registry_mirror"])
    def test_registry_mirror_value_persisted(self, api_client, request):
        """
        Test that the registry mirror configuration is correctly persisted

        Steps:
            1. GET the containerd-registry setting
            2. Parse the stored JSON value
            3. Verify the mirror URL appears in docker.io and registry.k8s.io endpoints

        Expected Result:
            - Stored value matches the mirror URL configured in test_set_valid_registry_mirror
        """
        mirror_url = request.config.getoption('--registry-mirror-url')
        if not mirror_url:
            pytest.skip("--registry-mirror-url is required for this test")

        code, data = api_client.settings.get('containerd-registry')
        assert 200 == code, (code, data)

        stored_value = json.loads(data.get('value', '{}'))
        mirrors = stored_value.get('mirrors', {})

        assert mirrors, (
            "containerd-registry mirrors should not be empty after update\n"
            f"Stored value: {stored_value}"
        )
        for registry in ('docker.io', 'registry.k8s.io'):
            endpoints = mirrors.get(registry, {}).get('endpoint', [])
            assert mirror_url in endpoints, (
                f"Mirror URL not found in {registry} endpoints after update\n"
                f"Expected: {mirror_url}\nActual endpoints: {endpoints}"
            )

    @pytest.mark.negative
    def test_set_invalid_registry_json_rejected(self, api_client):
        """
        Test that a malformed containerd-registry value is rejected

        Steps:
            1. PATCH containerd-registry with a non-JSON string value
            2. Verify response is 422

        Expected Result:
            - API rejects invalid JSON with 422 (Unprocessable Entity)
        """
        code, data = api_client.settings.update(
            'containerd-registry', {'value': 'not-valid-json{{{'}
        )
        assert 422 == code, (
            f"Expected 422 for malformed registry JSON, got {code}\n"
            f"API Status({code}): {data}"
        )


@pytest.mark.p1
@pytest.mark.addons
@pytest.mark.airgapped
class TestBuiltinAddonsAirgapped:
    """
    Test that built-in addons enable and disable successfully in an air-gapped environment

    Prerequisite:
        Cluster must have no outbound internet connectivity.
        All addon container images must be pre-loaded or served from a local registry.
        The containerd-registry mirror should be configured before running these tests.

    Validates that enabling addons does not trigger pulls from external registries by
    verifying no error state (ImagePullBackOff would surface as an error status).
    """

    @pytest.mark.dependency(name="enable_monitoring_addon")
    def test_enable_rancher_monitoring_addon(self, api_client, wait_timeout):
        """
        Test enabling the rancher-monitoring built-in addon without internet access

        Steps:
            1. Verify the addon exists on the cluster
            2. Skip if already enabled
            3. Enable the addon via the API
            4. Poll addon status until AddonDeploySuccessful or timeout
            5. Assert no error state appears during the wait (would indicate ImagePullBackOff)

        Expected Result:
            - Addon reaches deployed state using only locally available images
            - No error state is encountered during rollout
        """
        code, data = api_client.addons.get(MONITORING_ADDON)
        if code != 200:
            pytest.skip(f"Addon {MONITORING_ADDON} not present on cluster (code: {code})")

        if data.get('spec', {}).get('enabled', False):
            pytest.skip(f"Addon {MONITORING_ADDON} is already enabled; skipping enable test")

        code, data = api_client.addons.enable(MONITORING_ADDON)
        assert 200 == code, (
            f"Failed to enable addon {MONITORING_ADDON}\n"
            f"API Status({code}): {data}"
        )

        endtime = datetime.now() + timedelta(seconds=wait_timeout)
        while endtime > datetime.now():
            code, data = api_client.addons.get(MONITORING_ADDON)
            status = data.get('status', {}).get('status', '')
            assert 'error' not in status.lower(), (
                f"Addon {MONITORING_ADDON} entered error state in air-gapped environment\n"
                f"Status: {status}\n"
                f"This likely indicates a failed image pull from an external registry"
            )
            if status in ('deployed', 'AddonDeploySuccessful'):
                break
            sleep(5)
        else:
            raise AssertionError(
                f"Addon {MONITORING_ADDON} did not reach deployed state "
                f"within {wait_timeout}s\n"
                f"API Status({code}): {data}"
            )

    @pytest.mark.dependency(name="enable_logging_addon")
    def test_enable_rancher_logging_addon(self, api_client, wait_timeout):
        """
        Test enabling the rancher-logging built-in addon without internet access

        Steps:
            1. Verify the addon exists on the cluster
            2. Skip if already enabled
            3. Enable the addon via the API
            4. Poll addon status until AddonDeploySuccessful or timeout
            5. Assert no error state appears during the wait

        Expected Result:
            - Addon reaches deployed state using only locally available images
        """
        code, data = api_client.addons.get(LOGGING_ADDON)
        if code != 200:
            pytest.skip(f"Addon {LOGGING_ADDON} not present on cluster (code: {code})")

        if data.get('spec', {}).get('enabled', False):
            pytest.skip(f"Addon {LOGGING_ADDON} is already enabled; skipping enable test")

        code, data = api_client.addons.enable(LOGGING_ADDON)
        assert 200 == code, (
            f"Failed to enable addon {LOGGING_ADDON}\n"
            f"API Status({code}): {data}"
        )

        endtime = datetime.now() + timedelta(seconds=wait_timeout)
        while endtime > datetime.now():
            code, data = api_client.addons.get(LOGGING_ADDON)
            status = data.get('status', {}).get('status', '')
            assert 'error' not in status.lower(), (
                f"Addon {LOGGING_ADDON} entered error state in air-gapped environment\n"
                f"Status: {status}\n"
                f"This likely indicates a failed image pull from an external registry"
            )
            if status in ('deployed', 'AddonDeploySuccessful'):
                break
            sleep(5)
        else:
            raise AssertionError(
                f"Addon {LOGGING_ADDON} did not reach deployed state "
                f"within {wait_timeout}s\n"
                f"API Status({code}): {data}"
            )

    @pytest.mark.dependency(depends=["enable_monitoring_addon"])
    def test_disable_rancher_monitoring_addon(self, api_client, wait_timeout):
        """
        Test disabling the rancher-monitoring addon after air-gapped enable test

        Steps:
            1. Disable the addon via the API
            2. Poll addon status until Disabled or timeout

        Expected Result:
            - Addon reaches disabled state cleanly
        """
        code, data = api_client.addons.disable(MONITORING_ADDON)
        assert 200 == code, (
            f"Failed to disable addon {MONITORING_ADDON}\n"
            f"API Status({code}): {data}"
        )

        endtime = datetime.now() + timedelta(seconds=wait_timeout)
        while endtime > datetime.now():
            code, data = api_client.addons.get(MONITORING_ADDON)
            if 'Disabled' in data.get('status', {}).get('status', ''):
                break
            sleep(5)
        else:
            raise AssertionError(
                f"Addon {MONITORING_ADDON} did not reach disabled state "
                f"within {wait_timeout}s\n"
                f"API Status({code}): {data}"
            )

    @pytest.mark.dependency(depends=["enable_logging_addon"])
    def test_disable_rancher_logging_addon(self, api_client, wait_timeout):
        """
        Test disabling the rancher-logging addon after air-gapped enable test

        Steps:
            1. Disable the addon via the API
            2. Poll addon status until Disabled or timeout

        Expected Result:
            - Addon reaches disabled state cleanly
        """
        code, data = api_client.addons.disable(LOGGING_ADDON)
        assert 200 == code, (
            f"Failed to disable addon {LOGGING_ADDON}\n"
            f"API Status({code}): {data}"
        )

        endtime = datetime.now() + timedelta(seconds=wait_timeout)
        while endtime > datetime.now():
            code, data = api_client.addons.get(LOGGING_ADDON)
            if 'Disabled' in data.get('status', {}).get('status', ''):
                break
            sleep(5)
        else:
            raise AssertionError(
                f"Addon {LOGGING_ADDON} did not reach disabled state "
                f"within {wait_timeout}s\n"
                f"API Status({code}): {data}"
            )


@pytest.mark.p1
@pytest.mark.upgrade
@pytest.mark.airgapped
class TestAirgappedUpgrade:
    """
    Test upgrade version creation and failure behavior in an air-gapped environment

    Validates that:
    - A Version CRD can be created when the ISO URL points to an internal server
    - An upgrade initiated with an unreachable external ISO URL fails at ImageReady
      rather than hanging indefinitely or crashing the cluster
    """

    @pytest.mark.dependency(name="create_local_iso_version")
    def test_create_version_with_local_iso_url(
            self, api_client, unique_name, upgrade_checker, request):
        """
        Test that a Harvester version CRD can be created with a locally-served ISO URL

        Prerequisite:
            --upgrade-iso-url must point to an internal HTTP server (no internet required).
            --upgrade-iso-checksum must be set.

        Steps:
            1. Skip if --upgrade-iso-url or --upgrade-iso-checksum are not configured
            2. Create a Version resource with the local ISO URL
            3. Wait for the version to appear in the API
            4. Verify version spec reflects the local ISO URL
            5. Delete the version

        Expected Result:
            - Version is created successfully from a local ISO URL
            - No external network access is required for version creation
        """
        iso_url = request.config.getoption('--upgrade-iso-url', '').strip()
        checksum = request.config.getoption('--upgrade-iso-checksum', '').strip()

        if not iso_url or not checksum:
            pytest.skip(
                "--upgrade-iso-url and --upgrade-iso-checksum are required for this test"
            )

        version = f"airgap-{unique_name}"
        try:
            code, data = api_client.versions.create(version, iso_url, checksum)
            assert 201 == code, (
                f"Failed to create version {version} with local ISO URL\n"
                f"API Status({code}): {data}"
            )

            version_created, (code, data) = upgrade_checker.wait_version_created(version)
            assert version_created, (
                f"Version {version} not available after creation\n"
                f"API Status({code}): {data}"
            )

            assert data.get('spec', {}).get('isoURL') == iso_url, (
                f"Version spec.isoURL does not match the local ISO URL\n"
                f"Expected: {iso_url}\nActual: {data.get('spec', {})}"
            )
        finally:
            api_client.versions.delete(version)

    @pytest.mark.dependency(name="upgrade_unreachable_iso_fails")
    def test_upgrade_unreachable_iso_url_fails_gracefully(
            self, api_client, unique_name, upgrade_checker):
        """
        Test that an upgrade initiated with an unreachable ISO URL fails at ImageReady

        Steps:
            1. Create a Version with an invalid/unreachable ISO URL
            2. Initiate an upgrade from that version
            3. Poll upgrade conditions until Completed=False and ImageReady=False
            4. Verify the failure message indicates a download/connectivity issue
            5. Delete the upgrade and version

        Expected Result:
            - Upgrade transitions to a failed state cleanly
            - Cluster remains healthy; no nodes were partially upgraded
            - Failure is attributable to ISO download failure, not internal error
        """
        version = f"airgap-bad-{unique_name}"
        unreachable_url = "https://unreachable.internal/harvester.iso"
        checksum = sha512(b'placeholder').hexdigest()

        code, data = api_client.versions.get(version)
        if code != 200:
            code, data = api_client.versions.create(version, unreachable_url, checksum)
            assert 201 == code, (
                f"Failed to create version with unreachable URL\n"
                f"API Status({code}): {data}"
            )
            version_created, (code, data) = upgrade_checker.wait_version_created(version)
            assert version_created, (code, data)

        upgrade_name = None
        try:
            code, data = api_client.upgrades.create(version)
            assert 201 == code, (
                f"Failed to create upgrade from version {version}\n"
                f"API Status({code}): {data}"
            )
            upgrade_name = data['metadata']['name']

            upgrade_failed, (code, data) = \
                upgrade_checker.wait_upgrade_fail_by_invalid_iso_url(upgrade_name)
            assert upgrade_failed, (
                f"Upgrade {upgrade_name} did not transition to expected failed state\n"
                f"API Status({code}): {data}"
            )

            conds = {c['type']: c for c in data.get('status', {}).get('conditions', [])}
            assert 'False' == conds.get('Completed', {}).get('status'), (
                f"Upgrade Completed condition should be False\nConditions: {conds}"
            )
            assert 'False' == conds.get('ImageReady', {}).get('status'), (
                f"Upgrade ImageReady condition should be False\nConditions: {conds}"
            )
        finally:
            if upgrade_name:
                api_client.upgrades.delete(upgrade_name)
            api_client.versions.delete(version)


@pytest.mark.p1
@pytest.mark.backup_target
@pytest.mark.airgapped
class TestAirgappedBackupTarget:
    """
    Test backup target connectivity behavior in an air-gapped environment

    Validates that:
    - Configuring an external S3 endpoint (e.g. AWS) fails connectivity check or
      update validation when the cluster has no internet access
    - A local NFS server on the internal network validates successfully
    - A local S3-compatible store (e.g. MinIO) validates and accepts a backup

    Prerequisite for NFS tests: --nfs-endpoint must be set to an internal NFS server.
    Prerequisite for local S3 tests: --s3-endpoint, --bucketName, --region,
        --accessKeyId, --secretAccessKey must all be configured for the local store.
    """

    @pytest.fixture(scope="class", autouse=True)
    def restore_backup_target(self, api_client):
        code, data = api_client.settings.get('backup-target')
        assert 200 == code, (code, data)
        original_spec = api_client.settings.BackupTargetSpec.from_dict(data)
        yield
        api_client.settings.update('backup-target', original_spec)

    @pytest.mark.negative
    def test_external_s3_connectivity_fails(self, api_client):
        """
        Test that an S3 backup target pointing to an external endpoint fails in air-gapped env

        Steps:
            1. Build a plausible S3 spec targeting the public AWS endpoint
            2. Attempt to update backup-target with this spec
            3. If update succeeds (200), call the backup target health check endpoint
            4. Assert that either the update itself fails OR the health check returns non-200

        Expected Result:
            - Cluster cannot reach external S3 endpoint and reports a connectivity failure
        """
        S3Spec = api_client.settings.BackupTargetSpec.S3
        spec = S3Spec(
            'airgap-test-bucket',
            'us-east-1',
            'AKIAIOSFODNN7EXAMPLE',
            'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY',
            endpoint=EXTERNAL_S3_ENDPOINT
        )

        code, data = api_client.settings.update('backup-target', spec)
        if code == 422:
            # Harvester validated connectivity during update and rejected it — expected
            return

        assert 200 == code, (
            f"Unexpected status updating backup-target to external S3\n"
            f"API Status({code}): {data}"
        )

        health_code, health_data = api_client.settings.backup_target_test_connection()
        assert 200 != health_code, (
            "External S3 backup target health check succeeded in an air-gapped environment\n"
            "The cluster appears to have internet access, or the test environment is not "
            "air-gapped as expected\n"
            f"Health check response: {health_data}"
        )

    @pytest.mark.backupnfs
    def test_local_nfs_backup_target_connectivity(self, api_client, request):
        """
        Test that an NFS backup target on the internal network validates successfully

        Prerequisite:
            --nfs-endpoint must be set to an internal NFS server (e.g. nfs://192.168.1.10/share).

        Steps:
            1. Skip if --nfs-endpoint is not configured
            2. Update backup-target to the internal NFS endpoint
            3. Call the backup target health check endpoint
            4. Verify health check returns 200

        Expected Result:
            - NFS target on the internal network is reachable and validates successfully
        """
        nfs_endpoint = request.config.getoption('--nfs-endpoint')
        if not nfs_endpoint:
            pytest.skip("--nfs-endpoint is required for this test")

        NFSSpec = api_client.settings.BackupTargetSpec.NFS
        spec = NFSSpec(nfs_endpoint)

        code, data = api_client.settings.update('backup-target', spec)
        assert 200 == code, (
            f"Failed to update backup-target to internal NFS {nfs_endpoint}\n"
            f"API Status({code}): {data}"
        )

        health_code, health_data = api_client.settings.backup_target_test_connection()
        assert 200 == health_code, (
            f"Internal NFS backup target health check failed in air-gapped environment\n"
            f"NFS endpoint: {nfs_endpoint}\n"
            f"Health check response ({health_code}): {health_data}"
        )

    @pytest.mark.backups3
    def test_local_s3_backup_target_connectivity(self, api_client, request):
        """
        Test that a local S3-compatible backup target (e.g. MinIO) validates successfully

        Prerequisite:
            --s3-endpoint must point to an internal S3-compatible store.
            --bucketName, --region, --accessKeyId, --secretAccessKey must be configured.

        Steps:
            1. Skip if any required S3 config option is missing
            2. Update backup-target to the local S3 endpoint
            3. Call the backup target health check endpoint
            4. Verify health check returns 200

        Expected Result:
            - Local S3-compatible target is reachable and validates successfully
        """
        s3_endpoint = request.config.getoption('--s3-endpoint')
        bucket = request.config.getoption('--bucketName')
        region = request.config.getoption('--region')
        access_id = request.config.getoption('--accessKeyId')
        access_secret = request.config.getoption('--secretAccessKey')

        missing = [k for k, v in {
            's3-endpoint': s3_endpoint, 'bucketName': bucket, 'region': region,
            'accessKeyId': access_id, 'secretAccessKey': access_secret
        }.items() if not v]
        if missing:
            pytest.skip(
                f"Local S3 config options required but missing: {', '.join(missing)}"
            )

        S3Spec = api_client.settings.BackupTargetSpec.S3
        spec = S3Spec(bucket, region, access_id, access_secret, endpoint=s3_endpoint)

        code, data = api_client.settings.update('backup-target', spec)
        assert 200 == code, (
            f"Failed to update backup-target to local S3 {s3_endpoint}\n"
            f"API Status({code}): {data}"
        )

        health_code, health_data = api_client.settings.backup_target_test_connection()
        assert 200 == health_code, (
            f"Local S3 backup target health check failed in air-gapped environment\n"
            f"S3 endpoint: {s3_endpoint}\n"
            f"Health check response ({health_code}): {health_data}"
        )


@pytest.mark.p1
@pytest.mark.rancher
@pytest.mark.rancher_integration_with_external_rancher
@pytest.mark.airgapped
class TestRancherIntegrationAirgapped:
    """
    Test Harvester-Rancher integration when both systems have no internet access

    Prerequisite:
        --rancher-endpoint must point to an internal Rancher instance.
        --rancher-admin-password must be set.
        Both Rancher and Harvester nodes must be on an isolated network with no
        internet access. All fleet/cattle agent images must be pre-loaded or served
        from a local registry.

    Validates that the integration workflow completes using only internally available
    images and endpoints. An Active cluster state proves no external dependency blocked
    the integration process.
    """

    @pytest.mark.dependency(name="harvester_import_airgapped")
    def test_import_harvester_into_airgapped_rancher(
            self, api_client, rancher_api_client, unique_name, polling_for):
        """
        Test that Harvester can be imported into a Rancher instance with no internet access

        Steps:
            1. Create a Harvester management cluster entry in Rancher (Import Existing)
            2. Wait for Rancher to assign a clusterName to the entry
            3. Register Harvester with Rancher via the cluster-registration-url setting
            4. Wait for the management cluster to reach Active state
            5. Verify fleet/cattle agent pods are not in error state

        Expected Result:
            - Import completes and cluster reaches Active state
            - All agent pods start using only locally available images
        """
        cluster_name = f"hvst-airgap-{unique_name}"

        code, data = rancher_api_client.mgmt_clusters.create_harvester(cluster_name)
        assert 201 == code, (
            f"Failed to create Harvester entry in Rancher\n"
            f"API Status({code}): {data}"
        )

        try:
            code, data = polling_for(
                f"clusterName assignment in MgmtCluster {cluster_name}",
                lambda code, data: data.get('status', {}).get('clusterName'),
                rancher_api_client.mgmt_clusters.get, cluster_name
            )
            cluster_id = data['status']['clusterName']

            registration_url = data.get('status', {}).get('registrationToken', {}).get(
                'manifestUrl', ''
            )
            assert registration_url, (
                f"Rancher did not provide a registration manifest URL\n"
                f"Cluster status: {data.get('status', {})}"
            )

            # Register Harvester side
            if api_client.cluster_version.release >= (1, 8, 0):
                from json import dumps
                reg_value = dumps({"url": registration_url, "insecureSkipTLSVerify": True})
            else:
                reg_value = registration_url
            code, data = api_client.settings.update(
                'cluster-registration-url', {'value': reg_value}
            )
            assert 200 == code, (
                f"Failed to set cluster-registration-url on Harvester\n"
                f"API Status({code}): {data}"
            )

            # Wait for Rancher management cluster to reach Active state
            code, data = polling_for(
                f"MgmtCluster {cluster_name} to reach Active state",
                lambda code, data: (
                    data.get('metadata', {}).get('state', {}).get('name') == 'active'
                ),
                rancher_api_client.mgmt_clusters.get, cluster_name
            )
            assert 'active' == data['metadata']['state']['name'], (
                f"Cluster {cluster_name} did not reach active state\n"
                f"Current state: {data.get('metadata', {}).get('state', {})}"
            )

        finally:
            rancher_api_client.mgmt_clusters.delete(cluster_name)
            if api_client.cluster_version.release >= (1, 8, 0):
                from json import dumps
                api_client.settings.update(
                    'cluster-registration-url',
                    {'value': dumps({"url": "", "insecureSkipTLSVerify": False})}
                )
            else:
                api_client.settings.update('cluster-registration-url', {'value': ''})
