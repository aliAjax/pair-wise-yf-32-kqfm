import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class ReassignmentFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, expires_days=2):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East", "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=expires_days)), "clinical_match": 8})

    def candidate(self, name="患者甲", hospital="H2", urgency=5, wait=500):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital, "region": "East", "urgency": urgency, "wait_days": wait, "willing": True, "clinical_match": 9})

    def in_transit(self):
        donor, first = self.donor(), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"]})
        self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.0})
        return donor, first, allocation

    def test_reject_then_reassign_excludes_rejected_hospital(self):
        donor, first, allocation = self.in_transit()
        second = self.candidate("患者乙", "H3", 4, 300)
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "  "})
        self.assertEqual(ctx.exception.code, "reason_required")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject(allocation["id"], "hospital-h3", "hospital", "H3", {"reason": "停诊"})
        self.assertEqual(ctx.exception.status, 403)
        rejected = self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "接收医院临时停诊"})
        self.assertEqual(rejected["status"], "rejected")
        rank = self.svc.ranking(donor["id"], "allocation_officer", "")
        self.assertEqual(rank["donor"]["status"], "available")
        hospitals = {item["hospital"] for item in rank["candidates"]}
        self.assertNotIn("H2", hospitals)
        self.assertIn("H3", hospitals)
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"]})
        self.assertEqual(ctx.exception.code, "hospital_rejected")
        new_allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": second["id"]})
        self.assertNotEqual(new_allocation["id"], allocation["id"])
        timeline = self.svc.reassignments(donor["id"], "coordinator", "")["reassignments"]
        self.assertEqual(len(timeline), 1)
        record = timeline[0]
        self.assertEqual(record["from_allocation_id"], allocation["id"])
        self.assertEqual(record["from_hospital"], "H2")
        self.assertEqual(record["reason"], "接收医院临时停诊")
        self.assertEqual(record["rejected_by"], "hospital-h2")
        self.assertEqual(record["to_allocation_id"], new_allocation["id"])
        self.assertEqual(record["to_hospital"], "H3")
        self.assertEqual(record["reassigned_by"], "allocator")
        old = self.svc.get_allocation(allocation["id"], "auditor", "")
        self.assertEqual(old["status"], "rejected")
        self.assertEqual(old["reassignments"][0]["to_hospital"], "H3")
        self.assertEqual(new_allocation["reassignments"][0]["from_hospital"], "H2")
        with self.assertRaises(ApiError) as ctx:
            self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.0})
        self.assertEqual(ctx.exception.code, "allocation_closed")
        old_actions = [item["action"] for item in self.svc.audit(allocation["id"], "auditor")]
        self.assertEqual(old_actions, ["allocation_proposed", "allocation_accepted", "transfer_started", "allocation_rejected"])
        new_actions = [item["action"] for item in self.svc.audit(new_allocation["id"], "auditor")]
        self.assertEqual(new_actions, ["allocation_proposed", "reassignment_completed"])
        state = self.svc.state("coordinator", "")
        self.assertEqual([(r["from_hospital"], r["to_hospital"]) for r in state["reassignments"]], [("H2", "H3")])
        self.assertEqual(self.svc.state("hospital", "H2")["reassignments"][0]["to_hospital"], "H3")
        self.assertEqual(self.svc.state("hospital", "H9")["reassignments"], [])
        self.assertEqual(self.svc.state("viewer", "")["reassignments"], [])

    def test_reject_requires_transit_and_unexpired_organ(self):
        donor, first = self.donor(), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "停诊"})
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.0})
        self.svc.repo.conn.execute("UPDATE donors SET expires_at=? WHERE id=?", (iso(self.now - timedelta(hours=1)), donor["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "停诊"})
        self.assertEqual(ctx.exception.code, "organ_expired")

    def test_second_rejection_extends_timeline(self):
        donor, first, allocation = self.in_transit()
        second = self.candidate("患者乙", "H3", 4, 300)
        self.svc.reject(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "临时停诊"})
        new_allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": second["id"]})
        self.svc.accept(new_allocation["id"], "hospital-h3", "hospital", "H3", {"expected_revision": 1})
        self.svc.mark_transit(new_allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 4.0})
        self.svc.reject(new_allocation["id"], "hospital-h3", "hospital", "H3", {"reason": "手术室占用"})
        timeline = self.svc.reassignments(donor["id"], "auditor", "")["reassignments"]
        self.assertEqual([r["from_hospital"] for r in timeline], ["H2", "H3"])
        self.assertIsNone(timeline[1]["to_hospital"])
        hospitals = {item["hospital"] for item in self.svc.ranking(donor["id"], "allocation_officer", "")["candidates"]}
        self.assertNotIn("H2", hospitals)
        self.assertNotIn("H3", hospitals)


if __name__ == "__main__": unittest.main()
