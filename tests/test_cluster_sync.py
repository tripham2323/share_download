import hashlib
import http.client
import io
import json
import socket
import ssl
import tempfile
import unittest
from base64 import urlsafe_b64encode
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from cluster_sync import ClusterError, ClusterService, ClusterSettings
from file_store import FileStore


class ClusterSyncTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.settings = ClusterSettings.create_coordinator(
            "127.0.0.1", port, bind_host="127.0.0.1"
        )
        self.s1, self.store1 = self.open_node(self.settings)

    def open_node(self, settings):
        root = self.root / settings.node_id
        store = FileStore(root / "files")
        service = ClusterService(settings, store, root / "cluster")
        self.addCleanup(service.close)
        service.start()
        return service, store

    def follower_settings(self, node_id):
        return ClusterSettings.from_json(self.settings.export_enrollment(node_id))

    def source(self, name, data):
        target = self.root / name
        target.write_bytes(data)
        return target

    def test_follower_publish_and_offline_catchup_survive_coordinator_restart(self):
        s2, store2 = self.open_node(self.follower_settings("S2"))
        source = self.source("Tiếng Việt.bin", bytes(range(256)) * 1024)
        published = s2.publish(source)
        self.assertEqual(("S1", "S2"), published.acknowledged)
        self.assertEqual(source.stat().st_size, published.file_size)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), published.file_sha256)
        self.assertEqual(source.read_bytes(), self.store1.get(source.name).path.read_bytes())
        self.assertEqual(source.read_bytes(), store2.get(source.name).path.read_bytes())

        self.s1.close()
        with self.assertRaises(ClusterError):
            s2.refresh()
        self.assertEqual(source.read_bytes(), store2.get(source.name).path.read_bytes())

        restored = ClusterSettings.from_json(self.settings.to_json())
        s1, _ = self.open_node(restored)
        self.assertEqual((published,), s1.refresh().releases)
        s3, store3 = self.open_node(self.follower_settings("S3"))
        self.assertIsNone(store3.get(source.name))
        caught_up = s3.refresh()
        self.assertEqual({}, caught_up.conflicts)
        self.assertEqual(("S1", "S2", "S3"), caught_up.releases[0].acknowledged)
        self.assertEqual(source.read_bytes(), store3.get(source.name).path.read_bytes())
        self.assertEqual(caught_up, s3.refresh())
        self.assertEqual(caught_up.releases[0], s2.publish(source))
        self.assertEqual(caught_up, s1.refresh())

    def test_concurrent_publication_of_same_release_is_idempotent(self):
        s2, _ = self.open_node(self.follower_settings("S2"))
        s3, _ = self.open_node(self.follower_settings("S3"))
        source = self.source("shared.bin", b"shared" * 32768)
        with ThreadPoolExecutor(max_workers=2) as threads:
            pending = [threads.submit(node.publish, source) for node in (s2, s3)]
            releases = [future.result() for future in pending]
        self.assertEqual(releases[0].release_id, releases[1].release_id)
        self.assertEqual(1, len(self.s1.refresh().releases))
        self.assertEqual(("S1", "S2", "S3"), self.s1.refresh().releases[0].acknowledged)

    def test_conflicting_replica_is_reported_without_replacing_local_file(self):
        s2, store2 = self.open_node(self.follower_settings("S2"))
        store2.put("manual.bin", io.BytesIO(b"local copy"))
        self.s1.publish(self.source("manual.bin", b"coordinator copy"))
        self.s1.publish(self.source("other.bin", b"independent copy"))
        snapshot = s2.refresh()
        self.assertEqual({"manual.bin"}, set(snapshot.conflicts))
        self.assertEqual(b"local copy", store2.get("manual.bin").path.read_bytes())
        self.assertEqual(b"independent copy", store2.get("other.bin").path.read_bytes())
        acks = {release.filename: release.acknowledged for release in snapshot.releases}
        self.assertEqual({"manual.bin": ("S1",), "other.bin": ("S1", "S2")}, acks)

    def test_same_name_different_contents_cannot_replace_a_committed_release(self):
        s2, _ = self.open_node(self.follower_settings("S2"))
        s3, _ = self.open_node(self.follower_settings("S3"))
        source = self.source("immutable.bin", b"first")
        first = s2.publish(source)
        source.write_bytes(b"different")
        with self.assertRaises(ClusterError):
            s3.publish(source)
        self.assertEqual((first,), self.s1.refresh().releases)
        self.assertEqual(b"first", self.store1.get(source.name).path.read_bytes())
        renamed = s3.publish(source, name="new-release.bin")
        self.assertEqual("new-release.bin", renamed.filename)
        self.assertEqual(b"different", self.store1.get(renamed.filename).path.read_bytes())
        self.assertEqual(b"different", source.read_bytes())

    def test_upload_with_wrong_hash_is_not_committed(self):
        filename, payload = "bad.bin", b"body"
        declared_hash = hashlib.sha256(b"not the body").hexdigest()
        identity = json.dumps([filename, len(payload), declared_hash], separators=(",", ":"))
        release_id = hashlib.sha256(identity.encode()).hexdigest()
        settings = self.follower_settings("S2")
        trust = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        trust.load_verify_locations(cadata=settings.certificate_pem)
        connection = http.client.HTTPSConnection(settings.host, settings.port, context=trust, timeout=3)
        self.addCleanup(connection.close)
        connection.request("PUT", f"/v1/releases/{release_id}", body=payload, headers={
            "Authorization": f"Bearer {settings.token}",
            "X-File-Name": urlsafe_b64encode(filename.encode()).decode(),
            "X-File-Sha256": declared_hash,
        })
        with connection.getresponse() as rejected:
            self.assertEqual(422, rejected.status)
        self.assertIsNone(self.store1.get(filename))
        self.assertEqual((), self.s1.refresh().releases)

    def test_new_enrollment_cannot_authenticate_to_an_existing_coordinator(self):
        unrelated = ClusterSettings.create_coordinator(self.settings.host, self.settings.port)
        enrollment = json.loads(unrelated.export_enrollment("S2"))
        enrollment["certificate_pem"] = json.loads(self.settings.export_enrollment("S2"))[
            "certificate_pem"
        ]
        unauthorized, _ = self.open_node(ClusterSettings.from_json(json.dumps(enrollment)))
        with self.assertRaisesRegex(ClusterError, "401"):
            unauthorized.refresh()
        self.assertEqual((), self.s1.refresh().releases)

    def test_acknowledgement_is_scoped_to_the_token_owner(self):
        self.s1.publish(self.source("scoped.bin", b"scoped"))
        settings = replace(self.follower_settings("S3"), token=self.follower_settings("S2").token)
        mismatched_node, _ = self.open_node(settings)
        with self.assertRaises(ClusterError):
            mismatched_node.refresh()
        self.assertEqual(("S1", "S2"), self.s1.refresh().releases[0].acknowledged)

    def test_local_files_are_not_replicated_until_released(self):
        self.store1.put("local.bin", io.BytesIO(b"local-only"))
        s2, store2 = self.open_node(self.follower_settings("S2"))
        self.assertEqual((), s2.refresh().releases)
        self.assertIsNone(store2.get("local.bin"))
        local = self.source("local.bin", b"local-only")
        published = self.s1.publish(local)
        self.assertEqual(published.release_id, s2.refresh().releases[0].release_id)
        self.assertEqual(b"local-only", store2.get("local.bin").path.read_bytes())

    def test_enrollment_pin_and_endpoint_identity_are_checked(self):
        other_settings = ClusterSettings.create_coordinator(self.settings.host, self.settings.port)
        other_enrollment = ClusterSettings.from_json(other_settings.export_enrollment("S2"))
        wrong_cert, _ = self.open_node(other_enrollment)
        with self.assertRaisesRegex(ClusterError, "CERTIFICATE_VERIFY_FAILED"):
            wrong_cert.refresh()
        wrong_name, _ = self.open_node(replace(self.follower_settings("S3"), host="localhost"))
        with self.assertRaisesRegex(ClusterError, "CERTIFICATE_VERIFY_FAILED"):
            wrong_name.refresh()

    def test_enrollment_contains_only_the_selected_nodes_secret(self):
        coordinator = json.loads(self.settings.to_json())
        s2 = json.loads(self.settings.export_enrollment("S2"))
        s3 = json.loads(self.settings.export_enrollment("S3"))
        self.assertNotIn("PRIVATE KEY", json.dumps(s2))
        self.assertEqual(self.settings.host, s2["host"])
        self.assertEqual("S2", s2["node_id"])
        self.assertNotEqual(s2["token"], s3["token"])
        self.assertNotIn(s3["token"], json.dumps(s2))
        self.assertIn(s2["token"], json.dumps(coordinator))
        self.assertIn(s3["token"], json.dumps(coordinator))
        with self.assertRaises(ClusterError):
            self.follower_settings("S2").export_enrollment("S3")
