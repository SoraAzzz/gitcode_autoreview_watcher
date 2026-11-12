# 离线回归测试：覆盖任务状态、用户确认、发布保护、白名单及 DFX；不访问真实模型。
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import watcher as w

HEAD='a'*40
BASE='b'*40
PR={'number':5,'state':'open','head':{'sha':HEAD},'base':{'sha':BASE}}
FINDING={'priority':'P1','path':'src/a.py','line':4,'title':'Example',
         'body':'Condition and consequence','evidence':'src/a.py caller','security_sensitive':False}

def report():
    return {'head_sha':HEAD,'status':'complete','summary':'Review','limitations':'Static only',
            'findings':[copy.deepcopy(FINDING)]}

class TestState(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'state.db'
        self.store=w.Store(self.path,'infra-test/gctest1')
    def tearDown(self): self.temp.cleanup()
    def discover(self):
        self.store.discover([{'number':1,'created_at':'2000-01-01T00:00:00Z'}], False)
        self.store.discover([{'number':1,'created_at':'2000-01-01T00:00:00Z'},
                             {'number':2,'created_at':'2099-01-01T00:00:00Z'}], False)
    def test_baseline_and_new_pr(self):
        self.discover()
        self.assertEqual(self.store.next_job(),2)
        self.assertEqual(self.store.summary()[0]['status'],'baseline')
    def test_old_reopened_pr_is_not_new(self):
        self.store.discover([])
        self.store.discover([{'number':1,'created_at':'2000-01-01T00:00:00Z'}])
        self.assertIsNone(self.store.next_job())
    def test_new_pr_waits_for_confirmation_by_default(self):
        self.store.discover([])
        self.store.discover([{'number':2,'created_at':'2099-01-01T00:00:00Z'}])
        self.assertEqual(self.store.pending_confirmations(),[2])
        self.assertIsNone(self.store.next_job())
    def test_approved_pr_is_queued(self):
        self.store.discover([])
        self.store.discover([{'number':2,'created_at':'2099-01-01T00:00:00Z'}])
        self.store.decide(2,True)
        self.assertEqual(self.store.next_job(),2)
    def test_baseline_requires_explicit_approval_and_survives_poll_and_restart(self):
        prs=[{'number':1,'created_at':'2000-01-01T00:00:00Z'},
             {'number':2,'created_at':'2000-01-01T00:00:00Z'}]
        self.store.discover(prs,False)
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.store.pending_confirmations(),[])
        self.store.decide(1,True)
        self.store.discover(prs)
        reopened=w.Store(self.path,'infra-test/gctest1')
        self.assertEqual(reopened.next_job(),1)
        self.assertEqual([item['status'] for item in reopened.summary()],['queued','baseline'])
    def test_baseline_cannot_be_declined_or_retried(self):
        self.store.discover([{'number':1,'created_at':'2000-01-01T00:00:00Z'}])
        with self.assertRaises(w.WatchError): self.store.decide(1,False)
        with self.assertRaises(w.WatchError): self.store.retry(1)
        self.assertEqual(self.store.summary()[0]['status'],'baseline')
        self.assertIsNone(self.store.next_job())
    def test_approval_rejects_missing_and_ineligible_jobs(self):
        with self.assertRaises(w.WatchError): self.store.decide(99,True)
        self.discover()
        for status in ('queued','running','done','closed','needs_attention','reviewed_only'):
            with self.subTest(status=status):
                self.store.finish(1,status)
                with self.assertRaises(w.WatchError): self.store.decide(1,True)
                self.assertEqual(self.store.summary()[0]['status'],status)
    def test_declined_pr_stays_out_of_queue(self):
        self.store.discover([])
        self.store.discover([{'number':2,'created_at':'2099-01-01T00:00:00Z'}])
        self.store.decide(2,False)
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.store.summary()[0]['status'],'declined')
    def test_invalid_first_poll_does_not_initialize(self):
        with self.assertRaises(ValueError): self.store.discover([{'number':1,'created_at':'broken'}])
        self.assertTrue(self.store.discover([]))
    def test_restart_preserves_done_and_baseline(self):
        self.discover()
        self.store.finish(2,'done')
        new=w.Store(self.path,'infra-test/gctest1')
        self.assertIsNone(new.next_job())
        self.assertEqual(new.summary()[1]['status'],'done')
    def test_restart_recovers_running_job(self):
        self.discover()
        self.store.begin(2)
        self.assertEqual(w.Store(self.path,'infra-test/gctest1').next_job(),2)
    def test_state_cannot_be_shared_across_repositories(self):
        with self.assertRaises(w.WatchError): w.Store(self.path,'infra-test/another')
    def test_retry_limit_and_backoff(self):
        self.discover()
        self.store.begin(2)
        self.store.fail(2,'timeout',2)
        self.assertIsNone(self.store.next_job())
        self.store.begin(2)
        self.store.fail(2,'timeout',2)
        self.assertEqual(self.store.summary()[1]['status'],'needs_attention')
    def test_second_instance_lock_rejected(self):
        with w.instance_lock(Path(self.temp.name)/'lock'):
            with self.assertRaises(w.WatchError):
                with w.instance_lock(Path(self.temp.name)/'lock'): pass

class TestValidation(unittest.TestCase):
    def test_new_side_hunk_lines(self):
        diff='--- a/a\n+++ b/a\n@@ -2,3 +2,4 @@\n context\n-old\n+new\n+new2\n context\n@@ -10 +11 @@\n-old\n+new\n'
        self.assertEqual(w.changed_lines(diff),{3,4,11})
    def test_added_content_that_looks_like_header(self):
        self.assertEqual(w.changed_lines('@@ -0,0 +1 @@\n+++text\n'),{1})
    def test_deletion_only_has_no_valid_lines(self):
        self.assertEqual(w.changed_lines('@@ -2,1 +1,0 @@\n-gone\n'),set())
    def test_valid_report(self):
        self.assertEqual(len(w.validate_report(report(),HEAD,{'src/a.py':{4}},5)),1)
    def test_invalid_locations(self):
        for location in ('/etc/a','../a','other.py'):
            value=report(); value['findings'][0]['path']=location
            with self.assertRaises(w.Attention): w.validate_report(value,HEAD,{'src/a.py':{4}},5)
    def test_context_line_rejected(self):
        with self.assertRaises(w.Attention): w.validate_report(report(),HEAD,{'src/a.py':{3}},5)
    def test_stale_report(self):
        with self.assertRaises(w.WatchError): w.validate_report(report(),BASE,{'src/a.py':{4}},5)
    def test_sensitive_report_held(self):
        value=report();value['findings'][0]['security_sensitive']=True
        with self.assertRaises(w.Attention):w.validate_report(value,HEAD,{'src/a.py':{4}},5)
    def test_incomplete_report_held(self):
        value=report();value['status']='incomplete'
        with self.assertRaises(w.Attention):w.validate_report(value,HEAD,{'src/a.py':{4}},5)
    def test_invalid_repo_or_sha_rejected(self):
        for repo in ('https://evil/repo','../..','owner/repo;cmd'):
            with self.assertRaises(w.WatchError): w.repo_name(repo)
        with self.assertRaises(w.WatchError): w.sha('--upload-pack=bad')
    def test_selected_model_is_passed_to_codex(self):
        args=w.codex_review_args('codex',{'model':'chosen-model'},Path('job'),Path('out'))
        self.assertEqual(args[args.index('--model')+1],'chosen-model')

class TestConfirmation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.watcher=w.Watcher({'repo':'infra-test/gctest1','require_confirmation':True},self.root,{})
        self.watcher.store.discover([])
        self.watcher.store.discover([{'number':5,'created_at':'2099-01-01T00:00:00Z'}])
        self.watcher.gc=lambda *args:{'number':5,'state':'open','merged':False,'draft':False,
                                      'title':'Review me','html_url':'https://example/pull/5'}
    def tearDown(self):self.temp.cleanup()
    def test_foreground_user_approval_queues_pr(self):
        fake_stdin=MagicMock();fake_stdin.isatty.return_value=True
        with patch.object(w.sys,'stdin',fake_stdin),patch('builtins.input',return_value='y'):
            self.watcher.confirm_pending()
        self.assertEqual(self.watcher.store.next_job(),5)
    def test_non_tty_leaves_pr_waiting(self):
        fake_stdin=MagicMock();fake_stdin.isatty.return_value=False
        with patch.object(w.sys,'stdin',fake_stdin):self.watcher.confirm_pending()
        self.assertEqual(self.watcher.store.pending_confirmations(),[5])
        self.assertIsNone(self.watcher.store.next_job())

class TestControlCommand(unittest.TestCase):
    def test_approve_command_works_while_watcher_lock_is_held(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root=Path(temp_dir);state=root/'runtime';config_path=root/'config.json'
            config={'repo':'infra-test/gctest1','repo_allowlist':['infra-test/gctest1'],'poll_seconds':300,'publish':True,
                    'require_confirmation':True,'state_dir':'runtime','gitcode_path':'',
                    'codex_path':'','git_path':'','model':'','git_timeout_seconds':600,
                    'review_timeout_seconds':1800,'max_attempts':3,'max_findings':5,
                    'max_diff_bytes':1000000}
            config_path.write_text(json.dumps(config),encoding='utf-8')
            store=w.Store(state/'state.sqlite3',config['repo'])
            store.discover([{'number':4,'created_at':'2000-01-01T00:00:00Z'}])
            store.discover([{'number':4,'created_at':'2000-01-01T00:00:00Z'},
                            {'number':5,'created_at':'2099-01-01T00:00:00Z'},
                            {'number':6,'created_at':'2099-01-01T00:00:00Z'}])
            store.decide(6,True);store.begin(6)
            with w.instance_lock(state/'watcher.lock'):
                for number in (4,5):
                    with patch.object(w.sys,'argv',['watcher.py','--config',str(config_path),'--approve-pr',str(number)]):
                        w.main()
            self.assertEqual(store.next_job(),4)
            statuses={item['number']:item['status'] for item in store.summary()}
            self.assertEqual(statuses[5],'queued')
            self.assertEqual(statuses[6],'running')

class TestPublishing(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.job=Path(self.temp.name)
        self.watcher=w.Watcher({'repo':'infra-test/gctest1'},self.job,{})
        self.posts=[];self.comments=[];self.current=copy.deepcopy(PR)
        self.fail_post=False
        self.watcher.gc=self.gc
    def tearDown(self): self.temp.cleanup()
    def gc(self,*args):
        if args[:2]==('pr','view'):return self.current
        if args[:2]==('pr','comments'):return self.comments
        if args[:2]==('pr','comment'):
            body=Path(args[args.index('--body-file')+1]).read_text(encoding='utf-8')
            self.posts.append(body)
            if self.fail_post:raise w.WatchError('timeout')
            self.comments.append({'id':33,'body':body,'comment_type':'diff_comment',
                                  'diff_position':{'start_new_line':4}})
            return {'id':33}
        raise AssertionError(args)
    def publish(self,known=None):
        return self.watcher.publish_one(PR,self.job,FINDING,[] if known is None else known)
    def test_tag_and_verified_inline_comment(self):
        self.assertEqual(self.publish(),33)
        self.assertTrue(self.posts[0].startswith('[codereview]'))
    def test_restart_does_not_duplicate_sent_comment(self):
        self.publish();self.publish()
        self.assertEqual(len(self.posts),1)
    def test_remote_marker_recovers_lost_local_state(self):
        self.publish()
        with self.watcher.store.connect() as db:db.execute('DELETE FROM posts')
        self.publish(self.comments)
        self.assertEqual(len(self.posts),1)
    def test_timeout_holds_instead_of_resending(self):
        self.fail_post=True
        with self.assertRaises(w.Attention):self.publish()
        with self.assertRaises(w.Attention):self.publish()
        self.assertEqual(len(self.posts),1)
    def test_head_changed_blocks_publish(self):
        self.current['head']['sha']='c'*40
        with self.assertRaises(w.Stale):self.publish()
        self.assertEqual(self.posts,[])
    def test_closed_pr_blocks_publish(self):
        self.current['state']='closed'
        with self.assertRaises(w.Stale):self.publish()
        self.assertEqual(self.posts,[])
    def test_base_changed_blocks_publish(self):
        self.current['base']['sha']='c'*40
        with self.assertRaises(w.Stale):self.publish()
    def test_ordinary_comment_response_not_accepted(self):
        original=self.gc
        def gc(*args):
            result=original(*args)
            if args[:2]==('pr','comments'):result[0]['comment_type']='pr_comment'
            return result
        self.watcher.gc=gc
        with self.assertRaises(w.Attention):self.publish()



class TestProcessing(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.watcher=w.Watcher({'repo':'infra-test/gctest1','publish':True,'require_confirmation':True,'max_findings':5,
                                'max_attempts':3,'poll_seconds':300},self.root,{})
        self.watcher.store.discover([])
        self.watcher.store.discover([{'number':5,'created_at':'2099-01-01T00:00:00Z'}])
        self.current=copy.deepcopy(PR)
        self.watcher.gc=self.gc
        self.watcher.prepare=lambda pr,job:(BASE,{'src/a.py':{4}})
        self.watcher.review=lambda *args:(report(),[copy.deepcopy(FINDING)])
        self.sent=[]
        self.watcher.publish_one=lambda *args:self.sent.append(1)
    def tearDown(self):self.temp.cleanup()
    def gc(self,*args):
        if args[:2]==('pr','view'):return self.current
        if args[:2]==('pr','comments'):return []
        raise AssertionError(args)
    def test_process_completes_and_persists_summary(self):
        self.watcher.process(5)
        self.assertEqual(self.watcher.store.summary()[0]['status'],'done')
        self.assertEqual(len(self.sent),1)
        self.assertEqual(len(list((self.root/'runs').glob('*/summary.json'))),1)

    def test_notification_failure_does_not_retry_published_job(self):
        self.watcher.on_result = MagicMock(side_effect=OSError('terminal closed'))
        self.watcher.process(5)
        self.assertEqual(self.watcher.store.summary()[0]['status'], 'done')
        self.assertEqual(len(self.sent), 1)
    def test_publish_false_never_posts(self):
        self.watcher.config['publish']=False
        self.watcher.process(5)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.watcher.store.summary()[0]['status'],'reviewed_only')
    def test_closed_pr_never_reviews(self):
        self.current['state']='closed'
        self.watcher.process(5)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.watcher.store.summary()[0]['status'],'closed')
    def test_draft_waits(self):
        self.current['draft']=True
        self.watcher.process(5)
        self.assertEqual(self.sent,[])
        self.assertIsNone(self.watcher.store.next_job())
    def test_reviewer_failure_retries_without_posting(self):
        def fail(*args):raise w.WatchError('failure')
        self.watcher.review=fail
        self.watcher.process(5)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.watcher.store.summary()[0]['status'],'queued')
    def test_sensitive_result_holds(self):
        def hold(*args):raise w.Attention('sensitive')
        self.watcher.review=hold
        self.watcher.process(5)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.watcher.store.summary()[0]['status'],'needs_attention')
    def test_retry_preserves_pending_markers(self):
        self.watcher.store.finish(5,'needs_attention')
        self.watcher.store.post('marker',5,'pending')
        self.watcher.store.retry(5)
        self.assertEqual(self.watcher.store.next_job(),5)
        self.assertEqual(self.watcher.store.post_state('marker'),'pending')



class TestAllowlist(unittest.TestCase):
    def test_select_allowed(self):
        c={'repo':'a/one','repo_allowlist':['a/one','b/two']}
        self.assertEqual(w.select_repository(c,'b/two'),'b/two')
    def test_reject_unlisted(self):
        with self.assertRaises(w.WatchError):
            w.select_repository({'repo':'a/one','repo_allowlist':['b/two']})
    def test_reject_missing_empty_invalid_duplicates(self):
        for allowed in (None, [], 'a/one', ['../..'], ['a/one','a/one']):
            with self.assertRaises(w.WatchError):
                w.select_repository({'repo':'a/one','repo_allowlist':allowed})

class TestDiagnostics(unittest.TestCase):
    def test_failure_saves_output_and_exit_code(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root)/'diagnostics'
            result=subprocess.CompletedProcess(['codex'], 7, 'events', 'permission denied')
            with patch.object(w.subprocess,'run',return_value=result):
                with self.assertRaisesRegex(w.WatchError,'permission denied'):
                    w.run(['codex'],root,diagnostics=directory)
            self.assertEqual(json.loads((directory/'execution.json').read_text())['exit_code'],7)
            self.assertEqual((directory/'stdout.log').read_text(),'events')
    def test_timeout_keeps_partial_output(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root)/'diagnostics'
            error=subprocess.TimeoutExpired(['codex'],1,output=b'partial',stderr=b'timeout')
            with patch.object(w.subprocess,'run',side_effect=error):
                with self.assertRaises(w.WatchError):
                    w.run(['codex'],root,diagnostics=directory)
            self.assertEqual((directory/'stdout.log').read_text(),'partial')
            self.assertEqual(json.loads((directory/'execution.json').read_text())['status'],'timeout')
    def test_redacts_environment_credentials_and_url(self):
        with patch.dict(w.os.environ,{'GC_TOKEN':'fixture-token-value'}):
            value=w.redact('fixture-token-value https://user:pass@example.com Authorization: Bearer abcdef')
        self.assertNotIn('fixture-token-value',value)
        self.assertNotIn('user:pass',value)
        self.assertNotIn('abcdef',value)

if __name__ == '__main__':
    unittest.main()
