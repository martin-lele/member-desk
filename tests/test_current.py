"""Current release regressions. Every test uses a new, isolated SQLite database."""
import contextlib
import datetime as dt
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class CurrentRelease(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="member-desk-public-test-")
        self.addCleanup(self.temp.cleanup)
        self.module_name = "_isolated_current_member_desk_test"
        spec = importlib.util.spec_from_file_location(self.module_name, ROOT / "app.py")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.app
        self.addCleanup(sys.modules.pop, self.module_name, None)
        with mock.patch.dict(os.environ, {"MEMBER_DATA_DIR": self.temp.name}):
            spec.loader.exec_module(self.app)
        self.app.init()
        self.assertEqual(self.app.DB_PATH.parent, Path(self.temp.name).resolve())

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(self.app.connect()) as connection:
            yield connection

    def seed_team(self):
        with self.app.transaction() as c:
            c.executemany("INSERT INTO stores VALUES(?,?,1)",
                          [("store-a", "示例门店A"), ("store-b", "示例门店B")])
            c.executemany("INSERT INTO employees VALUES(?,?,?,1)", [
                ("employee-manager", "示例店长", "store-a"),
                ("employee-a", "示例甲", "store-a"),
                ("employee-b", "示例乙", "store-b"),
                ("employee-c", "示例丙", "store-b"),
            ])
            for uid, role, status, store, employee in [
                ("admin", "admin", "active", "store-a", None),
                ("manager", "manager", "active", "store-a", "employee-manager"),
                ("staff", "staff", "active", "store-a", "employee-a"),
                ("pending-a", "staff", "pending", "store-a", None),
                ("pending-b", "staff", "pending", "store-b", None),
                ("pending-unassigned", "staff", "pending", None, None),
            ]:
                c.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?)", (
                    uid, uid, "示例" + uid, "unused-test-password-hash", role,
                    status, store, employee, self.app.now()))

    def user(self, uid):
        with self.db() as c:
            return dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def act(self, uid, **body):
        with self.app.transaction() as c:
            return self.app.perform(c, self.user(uid), body)

    def reject_action(self, uid, status, **body):
        with self.assertRaises(self.app.Problem) as raised:
            self.act(uid, **body)
        self.assertEqual(raised.exception.status, status)

    def seed_member(self, uid="member", practice=0):
        with self.app.transaction() as c:
            c.execute("INSERT INTO members(id,name,phone,birthday,identity,created,practice) "
                      "VALUES(?,?,?,?,?,?,?)", (
                          uid, "演示会员", "13900000001" if not practice else "13900000002",
                          "", "", self.app.now(), practice))

    def ledger(self, amount, *, employee="employee-a", store="store-a",
               kind="recharge", member="member", created="2026-10-31T16:00:00.000Z",
               attribution="staff"):
        lid = self.app.uid()
        with self.app.transaction() as c:
            name = c.execute("SELECT name FROM stores WHERE id=?", (store,)).fetchone()[0]
            ename = (c.execute("SELECT name FROM employees WHERE id=?", (employee,)).fetchone()[0]
                     if employee else "上门客户")
            after = self.app.balance(c, member) + amount
            c.execute("INSERT INTO ledger(id,member_id,amount,note,created,store_id,store_name,"
                      "attribution,employee_id,employee_name,operator_id,operator_name,"
                      "balance_after,entry_type,payment_method) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (lid, member, amount, "模拟测试", created, store, name, attribution,
                       employee, ename, "admin", "示例管理员", after, kind, "cash"))
        return lid

    def ranking(self):
        with self.db() as c:
            return self.app.recharge_leaderboard(
                c, dt.datetime(2026, 11, 1, 0, 30, tzinfo=self.app.CST))

    def test_fresh_database_is_empty_and_initialization_is_idempotent(self):
        self.app.init()
        with self.db() as c:
            for table in ("users", "stores", "employees", "members", "ledger",
                          "member_autopay_config", "member_autopay_orders"):
                self.assertEqual(c.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertTrue((Path(self.temp.name) / "setup-token.txt").is_file())

    def test_sync_cannot_be_enabled_with_either_mapping_identifier_missing(self):
        sync = self.app.member_payment_sync
        for method, item in ((0, 202), (101, 0)):
            with self.subTest(method=method, item=item):
                with mock.patch.multiple(sync, METHOD=method, ROOM_ITEM=item):
                    with mock.patch.object(self.app.AUTO, "client_factory") as client:
                        with self.assertRaises(self.app.hostex.Error):
                            self.app.AUTO.control("enable", {"id": "demo-admin"})
                        client.assert_not_called()

    def test_missing_source_mapping_cannot_match_an_order_without_a_source(self):
        import member_order_rules
        stays = [{"reservation_code": "demo-order", "status": "accepted",
                  "guest_phone": "13900000003", "custom_channel": {},
                  "rates": {"total_rate": {"amount": "100.00", "currency": "CNY"}}}]
        self.assertFalse(member_order_rules.assess_order(stays)["eligible"])
        stays[0]["custom_channel"] = {"id": 303}
        self.assertTrue(member_order_rules.assess_order(stays, source_id=303)["eligible"])

    def test_payment_classifier_requires_explicit_room_item_mapping(self):
        import member_payment_rules
        record = {"status": "paid", "direction": "income", "currency": "CNY",
                  "amount": "100.00", "payment_method_id": 101, "item_id": 202,
                  "reservation_code": "demo-order"}
        kwargs = {"balance_method_id": 101, "recharge_item_id": 404}
        self.assertEqual(member_payment_rules.classify(record, **kwargs)["kind"], "review")
        classified = member_payment_rules.classify(record, room_item_id=202, **kwargs)
        self.assertEqual((classified["kind"], classified["balance_delta"]),
                         ("member_payment", -10000))

    def test_manager_approves_own_store_and_links_roster(self):
        self.seed_team()
        result = self.act("manager", action="approve_user", id="pending-a",
                          store_id="store-a", frontdesk_verified=True)
        self.assertTrue(result["ok"])
        account = self.user("pending-a")
        self.assertEqual((account["status"], account["store_id"]), ("active", "store-a"))
        with self.db() as c:
            roster = c.execute("SELECT * FROM employees WHERE id=?",
                               (account["employee_id"],)).fetchone()
            self.assertEqual((roster["store_id"], roster["name"]),
                             ("store-a", account["name"]))
            evidence = c.execute("SELECT detail FROM audit WHERE action='frontdesk_identity_verified'").fetchone()[0]
            self.assertIn("manager_manual_review", evidence)

    def test_manager_cannot_approve_foreign_unassigned_or_move_accounts(self):
        self.seed_team()
        for target, payload in [
            ("pending-b", {"store_id": "store-a"}),
            ("pending-unassigned", {"store_id": "store-a"}),
            ("pending-a", {"store_id": "store-b"}),
            ("pending-a", {"employee_id": "employee-b"}),
        ]:
            with self.subTest(target=target, payload=payload):
                self.reject_action("manager", 403, action="approve_user", id=target,
                                   frontdesk_verified=True, **payload)
                self.assertEqual(self.user(target)["status"], "pending")

    def test_approval_requires_identity_confirmation_and_manager_role(self):
        self.seed_team()
        self.reject_action("manager", 400, action="approve_user", id="pending-a",
                           store_id="store-a")
        self.reject_action("staff", 403, action="approve_user", id="pending-a",
                           store_id="store-a", frontdesk_verified=True)
        self.reject_action("manager", 403, action="set_manager_role", id="staff", role="manager")
        self.assertEqual(self.user("pending-a")["status"], "pending")

    def test_manager_rejects_once_with_reason_and_audit(self):
        self.seed_team()
        with self.app.transaction() as c:
            c.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                      ("demo-session", "pending-a", "demo-csrf", 9999999999))
        body = dict(action="reject_user", id="pending-a", reason="申请身份未核实")
        self.assertTrue(self.act("manager", **body)["ok"])
        self.assertTrue(self.act("manager", **body)["replayed"])
        self.assertEqual(self.user("pending-a")["status"], "rejected")
        with self.db() as c:
            decision = c.execute("SELECT * FROM user_application_rejections WHERE user_id='pending-a'").fetchone()
            self.assertEqual((decision["reason"], decision["reviewer_id"]),
                             ("申请身份未核实", "manager"))
            self.assertEqual(c.execute("SELECT count(*) FROM audit WHERE action='reject_user'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(*) FROM sessions WHERE user_id='pending-a'").fetchone()[0], 0)
        self.reject_action("manager", 409, **{**body, "reason": "更改已处理决定"})
        self.reject_action("manager", 409, action="approve_user", id="pending-a",
                           store_id="store-a", frontdesk_verified=True)

    def test_manager_cannot_reject_other_store_or_active_accounts(self):
        self.seed_team()
        self.reject_action("manager", 403, action="reject_user", id="pending-b", reason="测试原因")
        self.reject_action("manager", 403, action="reject_user", id="pending-unassigned", reason="测试原因")
        self.reject_action("manager", 409, action="reject_user", id="staff", reason="测试原因")
        self.reject_action("manager", 400, action="reject_user", id="pending-a", reason="  ")
        self.assertEqual(self.user("pending-a")["status"], "pending")
        self.assertEqual(self.user("pending-b")["status"], "pending")

    def test_manual_spend_is_denied_without_balance_or_ledger_change(self):
        self.seed_team()
        self.seed_member()
        self.ledger(10000)
        with self.db() as c:
            before = (self.app.balance(c, "member"),
                      c.execute("SELECT count(*) FROM ledger").fetchone()[0])
        for role in ("admin", "manager", "staff"):
            self.reject_action(role, 403, action="transaction", id="member", kind="spend",
                               key=self.app.uid(), amount="25.00", store_id="store-a")
        with self.db() as c:
            self.assertEqual((self.app.balance(c, "member"),
                              c.execute("SELECT count(*) FROM ledger").fetchone()[0]), before)

    def test_ranking_uses_global_month_net_recharge_and_excludes_unrelated_entries(self):
        self.seed_team()
        self.seed_member()
        self.seed_member("practice-member", practice=1)
        self.ledger(20000)
        self.ledger(-5000, kind="recharge_refund")
        self.ledger(12000, employee="employee-b", store="store-b")
        self.ledger(12000, employee="employee-c", store="store-b")
        self.ledger(999999, created="2026-10-31T15:59:59.999Z")
        self.ledger(999999, created="2026-11-30T16:00:00.000Z")
        self.ledger(999999, member="practice-member")
        self.ledger(999999, employee=None, attribution="walkin")
        self.ledger(999999, kind="spend_refund")
        self.ledger(999999, kind="pms_refund")
        board = self.ranking()
        self.assertEqual(board["period"], "2026-11")
        self.assertEqual([(row["amount"], row["rank"]) for row in board["leaders"]],
                         [(15000, 1), (12000, 2), (12000, 2)])
        self.assertEqual({row["store"] for row in board["leaders"]}, {"示例门店A", "示例门店B"})
        for row in board["leaders"]:
            self.assertEqual(set(row), {"employee", "store", "amount", "rank"})

    def test_pending_refund_does_not_reduce_ranking_approved_refund_reduces_once(self):
        self.seed_team()
        self.seed_member()
        original = self.ledger(20000)
        request_id = self.app.uid()
        with mock.patch.object(self.app, "now", return_value="2026-11-01T00:00:00.000Z"):
            self.act("manager", action="request_refund", original_id=original,
                     key=request_id, amount="50.00", reason="模拟部分充值退款")
            self.assertEqual(self.ranking()["leaders"][0]["amount"], 20000)
            self.reject_action("manager", 403, action="review_refund", id=request_id,
                               decision="approved", refund_confirmed=True)
            result = self.act("admin", action="review_refund", id=request_id,
                              decision="approved", refund_confirmed=True)
            self.assertTrue(result["ok"])
            repeated = self.act("admin", action="review_refund", id=request_id,
                                decision="approved", refund_confirmed=True)
            self.assertTrue(repeated["replayed"])
        self.assertEqual(self.ranking()["leaders"][0]["amount"], 15000)
        with self.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM ledger WHERE entry_type='recharge_refund'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
