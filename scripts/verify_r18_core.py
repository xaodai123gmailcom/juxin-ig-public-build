"""Stability regressions plus local Chromium relation-list fixtures for Windows."""
import sys
import faulthandler
import unittest
from pathlib import Path
from run_backend_tests import TimedTestResult
faulthandler.enable()
faulthandler.dump_traceback_later(TimedTestResult.case_timeout, exit=True)
root=Path(__file__).resolve().parents[1]
for path in (root/'backend',root/'backend'/'tests'):sys.path.insert(0,str(path))
import test_stability_r18 as a,test_nurture_scheduler as b,test_account_surface as c
import test_follow_monitor as d,test_native_cloud as e,test_nurture_flow as f
import test_collection_long_run as g,test_long_run_storage_scaling as h
import test_relation_r20 as i
from test_core import RuntimeManagerTestCase
from test_nurture_wait_r82 import StudioWaitBudgetR82Tests, StudioWaitRuntimeR82Tests
suite=unittest.TestSuite()
for cls in [a.StabilityAsyncTests,a.StabilityDatabaseTests,a.StabilityPersistenceTests,b.NurtureSchedulerTests,StudioWaitBudgetR82Tests,StudioWaitRuntimeR82Tests,c.AccountSurfaceTests,d.FollowMonitorTests,e.NativeCloudTests,f.NurtureCountsTests,f.NurtureInteractionTests,g.CollectionLongRunTests,h.LongRunStorageScalingTestCase,i.RelationR20Tests,i.RelationDOMR20Tests]:
    for name in unittest.defaultTestLoader.getTestCaseNames(cls):
        if cls==e.NativeCloudTests and not any(word in name for word in ['cloud','backup','restore','upload','local_data','new_computer']):continue
        suite.addTest(cls(name))
for name in ['test_resume_window_rebuilds_missing_execution_from_checkpoint','test_conclusive_mode_unavailable_clears_recovery_generation','test_incomplete_relationship_list_retries_without_network_reconnect','test_single_window_pause_resume_stop_delete_never_controls_siblings','test_initial_window_failure_does_not_cancel_dynamic_healthy_worker','test_teardown_renews_leases_until_all_slow_profile_closes_finish','test_live_queue_immediate_window_append_does_not_start_duplicate_workers','test_stopped_target_resumes_original_task_from_preserved_checkpoint','test_one_offline_window_does_not_block_an_online_window']:
    suite.addTest(RuntimeManagerTestCase(name))
result=unittest.TextTestRunner(verbosity=2, resultclass=TimedTestResult).run(suite)
raise SystemExit(not result.wasSuccessful())
