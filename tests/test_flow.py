import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class OrganFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, expires_days=2):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East", "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=expires_days)), "clinical_match": 8})

    def candidate(self, name="患者甲", hospital="H2", urgency=5, wait=500):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital, "region": "East", "urgency": urgency, "wait_days": wait, "willing": True, "clinical_match": 9})

    def test_complete_allocation_and_cold_chain_flow(self):
        donor, candidate = self.donor(), self.candidate()
        rank = self.svc.ranking(donor["id"], "allocation_officer", "")
        self.assertEqual(rank["candidates"][0]["id"], candidate["id"])
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.assertEqual(transit["status"], "in_transit")
        handoff = self.svc.initiate_handoff(allocation["id"], "hospital-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.assertEqual(handoff["handoff"]["status"], "initiated")
        received = self.svc.accept_handoff(allocation["id"], "hospital-h2", "hospital", "H2", {})
        self.assertEqual(received["status"], "handed_off")
        implanted = self.svc.implant(allocation["id"], "allocator", "allocation_officer", {})
        self.assertEqual(implanted["status"], "implanted")
        audit = self.svc.audit(allocation["id"], "auditor")
        self.assertEqual([item["action"] for item in audit], ["allocation_proposed", "allocation_accepted", "transfer_started", "handoff_initiated", "handoff_accepted", "organ_implanted"])

    def test_expiry_privacy_and_single_allocation(self):
        expired = self.donor(expires_days=-1); candidate = self.candidate()
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": expired["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "organ_expired")
        donor2 = self.donor(); allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "wrong", "hospital", "H1", {"expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)
        masked = self.svc.get_allocation(allocation["id"], "hospital", "H1")
        self.assertEqual(masked["patient_name"], "***")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "donor_unavailable")
        other = self.candidate("患者乙", "H2", 4, 300)
        self.assertNotEqual(other["id"], candidate["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 12})
        self.assertEqual(ctx.exception.code, "cold_chain_violation")

    def test_reject_and_reassign_flow(self):
        donor, candidate = self.donor(), self.candidate()
        backup = self.candidate("患者丙", "H3", 4, 400)
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})

        rejected = self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "临时停诊，手术室不可用"})
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["rejected_reason"], "临时停诊，手术室不可用")
        self.assertEqual(rejected["rejected_by"], "hospital-h2")
        self.assertTrue(rejected["rejected_at"])
        self.assertTrue(rejected["organ_unexpired"])
        self.assertEqual(self.svc.state("coordinator", "")["donors"][0]["status"], "available")

        rank = self.svc.ranking(donor["id"], "allocation_officer", "")
        self.assertEqual([c["hospital"] for c in rank["candidates"]], ["H3"])
        self.assertEqual(rank["excluded_rejected_hospitals"], ["H2"])

        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "hospital_rejected")

        result = self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": backup["id"]})
        record = result["record"]
        self.assertEqual(record["from_hospital"], "H2")
        self.assertEqual(record["to_hospital"], "H3")
        self.assertEqual(record["reason"], "临时停诊，手术室不可用")
        self.assertEqual(record["rejected_by"], "hospital-h2")
        self.assertEqual(record["reassigned_by"], "allocator")
        self.assertEqual(record["old_allocation_id"], allocation["id"])
        self.assertEqual(record["new_allocation_id"], result["new_allocation"]["id"])
        self.assertEqual(result["old_allocation"]["status"], "rejected")
        self.assertEqual(result["new_allocation"]["status"], "proposed")
        self.assertEqual(result["new_allocation"]["candidate_hospital"], "H3")

        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": backup["id"]})
        self.assertEqual(ctx.exception.code, "already_reassigned")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": backup["id"]})
        self.assertEqual(ctx.exception.code, "donor_unavailable")

        accepted = self.svc.accept(result["new_allocation"]["id"], "hospital-h3", "hospital", "H3", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")

        audit = self.svc.audit(allocation["id"], "auditor")
        self.assertEqual([a["action"] for a in audit],
                         ["allocation_proposed", "allocation_accepted", "transfer_started", "allocation_rejected", "allocation_reassigned"])
        new_audit = self.svc.audit(result["new_allocation"]["id"], "auditor")
        self.assertEqual([a["action"] for a in new_audit], ["allocation_proposed", "allocation_accepted"])

        records = self.svc.reassignments(None, "coordinator")["reassignments"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["reassigned_at"], record["reassigned_at"])
        self.assertEqual(self.svc.reassignments(donor["id"], "auditor")["reassignments"][0]["to_hospital"], "H3")
        self.assertEqual(self.svc.reassignments(999, "coordinator")["reassignments"], [])

    def test_reject_and_reassign_guards(self):
        donor, candidate = self.donor(), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "太早"})
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "  "})
        self.assertEqual(ctx.exception.code, "reason_required")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject(allocation["id"], "hospital-h1", "hospital", "H1", {"reason": "越权"})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "not_rejected")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "coord", "coordinator", {"candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "reassign_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassignments(None, "hospital")
        self.assertEqual(ctx.exception.code, "reassignments_forbidden")

        self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "临时停诊"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "reassign_required")

        self.svc.repo.conn.execute("UPDATE donors SET expires_at=? WHERE id=?", (iso(self.now - timedelta(hours=1)), donor["id"]))
        backup = self.candidate("患者丙", "H3", 4, 400)
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": backup["id"]})
        self.assertEqual(ctx.exception.code, "organ_expired")
        self.assertEqual(self.svc.reassignments(None, "auditor")["reassignments"], [])
        self.assertEqual(self.svc.state("coordinator", "")["donors"][0]["status"], "available")


if __name__ == "__main__": unittest.main()
