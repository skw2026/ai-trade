#!/usr/bin/env python3
import copy
import unittest
from close_eth_vintage_diagnosis import verify_chain


class ChainEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.commits=[dict(sha='a'*40,parents=[dict(sha='b'*40)],
            commit=dict(committer=dict(date='2026-04-01T00:00:00Z'))),
            dict(sha='b'*40,parents=[],commit=dict(committer=dict(date='2026-03-01T00:00:00Z')))]
        self.branches=[dict(name='master',commit=dict(sha='a'*40))]
        self.tags=[]
        self.dates=('2023-09-18T12:00:00Z','2024-08-05T12:00:00Z')

    def verify(self):
        return verify_chain(self.commits,self.branches,self.tags,self.dates)

    def test_complete_chain_is_only_bounded_absence(self):
        r=self.verify()
        self.assertTrue(r['parent_chain_complete'])
        self.assertFalse(r['historical_publication_proven'])
        self.assertEqual(r['cause_of_missing_earlier_history'],'UNKNOWN')

    def test_unvisited_parent_rejected(self):
        self.commits[-1]['parents']=[dict(sha='c'*40)]
        with self.assertRaisesRegex(ValueError,'PARENT'):
            self.verify()

    def test_other_branch_or_tag_rejected(self):
        self.branches.append(dict(name='old',commit=dict(sha='c'*40)))
        with self.assertRaisesRegex(ValueError,'BRANCH'):
            self.verify()
        self.branches.pop()
        self.tags=[dict(name='archive-2023')]
        with self.assertRaisesRegex(ValueError,'TAGS'):
            self.verify()

    def test_older_commit_cannot_be_called_absent(self):
        self.commits[-1]['commit']['committer']['date']='2023-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'HISTORICAL'):
            self.verify()

    def test_duplicate_and_head_mismatch(self):
        original=copy.deepcopy(self.commits)
        self.commits.append(copy.deepcopy(self.commits[-1]))
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):
            self.verify()
        self.commits=original
        self.branches[0]['commit']['sha']='d'*40
        with self.assertRaisesRegex(ValueError,'BRANCH'):
            self.verify()

    def merged_history(self):
        self.commits=[dict(sha='a'*40,parents=[dict(sha='b'*40),dict(sha='c'*40)],
            commit=dict(committer=dict(date='2026-04-01T00:00:00Z'))),
            dict(sha='c'*40,parents=[dict(sha='d'*40)],
                 commit=dict(committer=dict(date='2026-03-30T00:00:00Z'))),
            dict(sha='b'*40,parents=[dict(sha='d'*40)],
                 commit=dict(committer=dict(date='2026-03-29T00:00:00Z'))),
            dict(sha='d'*40,parents=[],commit=dict(committer=dict(date='2026-03-01T00:00:00Z')))]

    def test_merge_and_nonadjacent_parents_allowed(self):
        self.merged_history()
        r=self.verify()
        self.assertEqual(r['merge_commit_count'],1)
        self.assertEqual(r['root_sha'],'d'*40)

    def test_missing_second_merge_parent_rejected(self):
        self.merged_history()
        self.commits.pop(1)
        with self.assertRaisesRegex(ValueError,'PARENT'):
            self.verify()

    def test_cycle_rejected(self):
        self.commits[-1]['parents']=[dict(sha='a'*40)]
        with self.assertRaisesRegex(ValueError,'CYCLE'):
            self.verify()

    def test_disconnected_commit_rejected(self):
        self.commits.append(dict(sha='c'*40,parents=[],commit=dict(committer=dict(date='2026-03-01T00:00:00Z'))))
        with self.assertRaisesRegex(ValueError,'UNREACHABLE'):
            self.verify()

    def test_shuffled_merge_interior_still_complete(self):
        self.merged_history()
        self.commits[1],self.commits[3]=self.commits[3],self.commits[1]
        self.assertEqual(self.verify()['root_sha'],'d'*40)


if __name__=='__main__':
    unittest.main()
