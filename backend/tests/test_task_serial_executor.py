import importlib.util
import pathlib
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "services" / "task_serial_executor.py"
spec = importlib.util.spec_from_file_location("task_serial_executor", MODULE_PATH)
if spec is None or spec.loader is None:
    raise ImportError("task_serial_executor module spec not found")
task_serial_executor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_serial_executor)
SerialTaskExecutor = task_serial_executor.SerialTaskExecutor


class TestTaskSerialExecutor(unittest.TestCase):
    """执行器现为线程池并发实现（TASK_MAX_WORKERS，默认 3）。

    历史上它是全局串行锁；改为并发后，旧断言「并发峰值 == 1」不再成立，
    这里更新为并发语义：所有任务都完成，且并发峰值不超过 max_workers。
    """

    def test_executor_runs_all_tasks_within_worker_limit(self):
        executor = SerialTaskExecutor(max_workers=2)
        state_lock = threading.Lock()
        state = {"active": 0, "peak_active": 0, "completed": 0}

        def work():
            with state_lock:
                state["active"] += 1
                state["peak_active"] = max(state["peak_active"], state["active"])
            time.sleep(0.05)
            with state_lock:
                state["active"] -= 1
                state["completed"] += 1

        threads = [threading.Thread(target=lambda: executor.run(work)) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(state["completed"], 6)
        self.assertGreaterEqual(state["peak_active"], 1)
        self.assertLessEqual(state["peak_active"], 2)

    def test_run_returns_result(self):
        executor = SerialTaskExecutor()
        self.assertEqual(executor.run(lambda: 42), 42)


if __name__ == "__main__":
    unittest.main()
