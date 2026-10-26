import copy
import unittest
import run_i4v


class ProducingGateBinding(unittest.TestCase):
    def test_healthy_and_frozen_partial_faults_bind_actual_elf(self):
        for count in (1,2):
            report=dict(case='partial-progress',owners=[dict(store_pc=4096,faults=count,
                fault=[dict(pc=4096),dict(pc=4096 if count==2 else 0)])])
            run_i4v.bind_partial_store(report,4096)
            with self.assertRaises(ValueError):run_i4v.bind_partial_store(report,8192)
            for index in range(count):
                changed=copy.deepcopy(report);changed['owners'][0]['fault'][index]['pc']+=4
                with self.assertRaises(ValueError):run_i4v.bind_partial_store(changed,4096)

if __name__=='__main__':unittest.main()
