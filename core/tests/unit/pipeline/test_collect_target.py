from tightrein.domain.enums import DeploymentStatus, Probe, RunStatus, Stage
from tightrein.pipeline.collect.steps import preconditions, target
from tightrein.store.repos import budget_usage, deployments
from pipeline_world import MAIN, RELEASE, FakeDeployments, FakeRefs, FakeTransport, deployment_run, make_world


def resolve(world, probe=Probe.API_FUZZ, deployments_reader=None, refs=None, **options):
    return target.resolve(world.config, world.layout, world.conn, probe,
                          deployments_reader=deployments_reader or FakeDeployments(deployment_run()),
                          refs=refs or FakeRefs(), clock=world.clock, **options)


def test_release_is_the_latest_deployment_and_is_recorded(tmp_path):
    world = make_world(tmp_path)
    info = resolve(world)
    assert info.release == RELEASE
    assert info.base_url == "https://staging.example.test"
    assert info.worktree is None and info.worktree_head is None
    saved = deployments.get(world.conn, RELEASE)
    assert saved.status is DeploymentStatus.SUCCEEDED and saved.workflow_run_id == "11"


def test_environment_comes_from_the_target_config(tmp_path):
    assert resolve(make_world(tmp_path / "default")).environment == "staging"
    world = make_world(tmp_path / "production", target={"baseUrl": "https://example.test", "environment": "production"})
    info = resolve(world)
    assert info.environment == "production"
    assert info.probe_target("run-1", tmp_path, world.clock).environment == "production"
    assert resolve(make_world(tmp_path / "none", target=None)).environment == "staging"


def test_without_deployment_the_release_is_unknown(tmp_path):
    world = make_world(tmp_path)
    assert resolve(world, deployments_reader=FakeDeployments(None)).release is None
    assert deployments.find(world.conn) == []


def test_commit_and_target_override(tmp_path):
    world = make_world(tmp_path)
    info = resolve(world, commit="c" * 40, target="http://localhost:5000", record=False)
    assert (info.release, info.base_url) == ("c" * 40, "http://localhost:5000")
    assert deployments.find(world.conn) == []


def test_static_targets_the_main_branch_and_reads_the_worktree_head(tmp_path):
    world = make_world(tmp_path)
    world.layout.readonly_worktree().mkdir(parents=True)
    refs = FakeRefs({("demo-repo", "origin/main"): MAIN, ("readonly", "HEAD"): MAIN})
    info = resolve(world, Probe.STATIC, refs=refs)
    assert (info.release, info.worktree_head) == (MAIN, MAIN)
    assert info.worktree == world.layout.readonly_worktree()


def test_health_check_passes_and_is_recorded(tmp_path):
    world = make_world(tmp_path)
    transport = FakeTransport(200, 35)
    gate = preconditions.check(world.config, world.conn, Probe.API_FUZZ, resolve(world), transport=transport,
                               clock=world.clock)
    assert gate.passed and gate.environment.health.status == 200
    assert transport.requests[0].url == "https://staging.example.test/health"
    assert transport.requests[0].timeout_seconds == 10


def test_failed_or_timed_out_health_check_blocks(tmp_path):
    world = make_world(tmp_path)
    for status in (503, None):
        gate = preconditions.check(world.config, world.conn, Probe.API_FUZZ, resolve(world),
                                   transport=FakeTransport(status), clock=world.clock)
        assert (gate.passed, gate.status, gate.reason) == (False, RunStatus.BLOCKED, "staging 不可用")
        assert gate.environment.health.status == status


def test_reparse_and_other_probes_skip_the_health_check(tmp_path):
    world = make_world(tmp_path)
    transport = FakeTransport(None)
    info = resolve(world)
    assert preconditions.check(world.config, world.conn, Probe.API_FUZZ, info, transport=transport,
                               clock=world.clock, reparse=True).passed
    assert preconditions.check(world.config, world.conn, Probe.PLATFORM_ERRORS, info, transport=transport,
                               clock=world.clock).passed
    assert transport.requests == []


def test_exhausted_budget_blocks_static(tmp_path):
    world = make_world(tmp_path)
    info = resolve(world, commit=MAIN)
    assert preconditions.check(world.config, world.conn, Probe.STATIC, info, transport=FakeTransport(),
                               clock=world.clock).passed
    today = world.clock.now().astimezone().date()
    budget_usage.add(world.conn, Stage.COLLECT, today, 2.0, 10, 10, False, world.clock)
    gate = preconditions.check(world.config, world.conn, Probe.STATIC, info, transport=FakeTransport(),
                               clock=world.clock)
    assert (gate.passed, gate.reason) == (False, "今日静态巡检预算已用尽")
