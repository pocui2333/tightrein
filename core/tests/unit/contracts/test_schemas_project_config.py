import pytest
from contract_samples import changed, paths, without

from tightrein.contracts import validate

CONFIG = "config/project-config.schema.json"


def tunable(value, low, high):
    return {"value": value, "min": low, "max": high}


PROJECT = {
    "project": {"name": "sample", "repo": "/Users/me/Projects/sample", "mainBranch": "main"},
    "target": {"baseUrl": "https://staging.example.test", "healthcheck": "/api/health", "healthTimeoutSeconds": 10,
               "manualDeployPaths": ["src/python/SampleNode"]},
    "accounts": {
        "roles": {"Company": {"keychain": "tightrein.sample.company"}, "Personal": {"keychain": "tightrein.sample.personal"}},
        "login": {"endpoint": "/api/Account/Login", "bodyTemplate": {"userName": "{account}", "password": "{password}"},
                  "tokenPath": "data.token"},
    },
    "stacks": ["webstack"],
    "extensions": {
        "spec-export": {"use": "core/openapi-file", "options": {"path": "docs/openapi.json"}},
        "authz-roles": {"command": ["{python}", "authz_roles.py"], "options": {"matrixFile": "src/Auth/Matrix.cs"}},
        "error-tracking": {"use": "core/sentry", "options": {"organization": "acme", "keychainItem": "s"}},
        "log-platform": {"command": ["{python}", "log_platform.py"], "timeoutSeconds": 60},
        "alert-source": {"use": "core/alertmanager", "options": {"url": "https://grafana.example.test"}},
        "log-parse": {"command": ["{python}", "log_parse_frames.py"], "mode": "extend"},
        "static-tools": {"command": ["{python}", "static_tools_frontend.py"], "mode": "extend", "options": {}},
        "page-routes": {"command": ["node", "page_routes.mjs"], "mode": "replace"},
        "local-run": {"enabled": False},
    },
    "sources": {
        "api-fuzz": {"exclude": ["^/api/Admin/"], "levels": {"shallow": {"maxExamples": 50, "phases": ["examples"]}},
                     "workers": 2, "sanitizeKeys": ["phone"], "timeoutMinutes": 30},
        "platform-errors": {"every": "1h", "initialLookbackHours": 24, "logQuery": '{app="api"} |= "error"',
                            "logLimit": 5000, "levels": ["error", "critical"]},
        "access-log": {"every": "1d", "query": '{app="gateway"}', "fields": {"method": "req.method", "route": "req.path",
                                                                            "status": "res.status",
                                                                            "durationMs": "elapsed"},
                       "pattern": None, "minRequests": 20, "latencyRatio": 2, "errorRateDelta": 0.05,
                       "baselineWeight": 0.3},
        "alerts": {"every": "15m", "exclude": {"names": ["(?i)^node"], "labels": {"category": ["infra"]}}},
        "project-probes": [{"name": "daily-import", "command": ["{python}", "probes/daily_import.py"], "every": "1d",
                            "keychain": ["tightrein.sample.db"], "timeoutSeconds": 120}],
        "static": {"semgrep": {"configs": ["p/csharp", "p/javascript"]}, "maxClaims": 10},
    },
    "checks": {"commands": [{"name": "frontend-build", "cwd": "src/vue", "command": "npm run build", "when": ["src/vue/**"],
                             "mustNotModify": False},
                            {"name": "unit", "cwd": ".", "command": "make unit", "when": ["src/**"],
                             "affected": {"map": [{"pattern": "^src/(.*)\\.src$", "test": "tests/\\1_test.src"}],
                                          "command": "make unit TESTS={tests}"}}],
               "prepare": [{"name": "npm-install", "cwd": "src/vue", "command": "npm ci"}],
               "residuePatterns": ["console\\.log"], "timeoutSeconds": 600,
               "pages": {"patrolGrep": "@browse", "locale": "zh-CN", "retries": 2, "timeoutMinutes": 20,
                         "ignoreRequests": [{"method": "GET", "pathPattern": "^/api/Admin/", "status": 403}]}},
    "localRun": {"ports": {"api": 5100, "backendForPages": 5000, "frontend": 8080}, "readyTimeoutSeconds": 120,
                 "stopTimeoutSeconds": 10, "readyUrlTimeoutSeconds": 5, "pollIntervalMs": 100},
    "review": {"riskRules": {"schema": {"paths": ["**/Migrations/**"], "patterns": ["modelBuilder."]}},
               "deepTriggers": {"categories": ["schema", "authz"]}},
    "protectedPaths": ["Migrations/**"], "protectedPatterns": ["\\[Authorize"], "testPaths": ["**/*.test.js"],
    "skipMarkers": ["@ts-ignore"], "credentialFiles": [".env", "*.pem"],
    "git": {"conventions": {"branch": "{prefix}{type}-{slug}", "commit": "{type}: {summary}", "prTemplate": None},
            "personalPrefix": True, "branchTypes": {"default": "fix", "feature": "feat"},
            "commitTypes": {"default": "fix"}, "inference": {"sampleSize": 50, "minSamples": 10, "minRatio": 0.8},
            "forbiddenPrefixes": ["claude"], "splitThreshold": 23, "worktreeLinks": ["src/vue/node_modules"]},
    "stages": {
        "triage": {"limits": {"maxTurns": 40}, "budgetPerDay": 5,
                   "roles": {"claim-verifier": {"limits": {"low": {"maxTurns": 20}, "high": {"maxTurns": 60}}}},
                   "tasks": {"dedup": {"limits": {"maxTurns": 10}}}},
        "fix": {"review": {"light": {"limits": {"maxTurns": 20}}, "deep": {"limits": {"maxTurns": 50}}},
                "budget": {"low": 2, "high": 6}},
        "verify": {"screenshotReview": {"limits": {"maxTurns": 10}}},
    },
    "models": {"opus": {"tool": "claude", "model": "claude-opus", "effort": "high", "inputUsdPerMTok": 15,
                        "outputUsdPerMTok": 75},
               "agy": {"tool": "agy"}},
    "routes": {"default": "opus", "triage.refuter": "agy", "fix.planner.high-risk": "opus"},
    "schedule": {"tick": {"weekdays": [1, 2, 3, 4, 5], "hours": [8, 13], "minutes": [0, 30]},
                 "onDeploy": [{"probe": "api-fuzz", "level": "shallow"}, {"probe": "platform-errors"}],
                 "tasks": [{"name": "static", "days": "workdays", "at": ["08:30", "13:30"], "command": "collect --probe static"}],
                 "weekly": {"at": "08:30"}, "nonWorkingDays": ["2026-10-12"]},
    "evaluation": {"repeats": 3, "parallelism": 1, "budgetUsd": 20},
    "thresholds": {
        "tiers": {"micro": {"maxFiles": tunable(1, 1, 5), "maxLines": tunable(30, 5, 200)}},
        "reproduceAttempts": tunable(2, 1, 5),
        "suppressionDays": tunable(30, 7, 90),
        "triage": {"perRun": tunable(5, 1, 10), "deferredReopenOccurrences": tunable(3, 1, 10)},
        "retrieval": {"contextLimits": {"static-review": tunable(20, 5, 40)}},
        "learn": {"controls": {"firstPassFloor": tunable(0.5, 0.1, 1)}},
    },
}
MINIMAL = {"project": PROJECT["project"]}
STATIC_ROLES = {"Learner": {"keychain": "tightrein.sample.learner"}}
STATIC_HEADER = {"kind": "static-header", "header": "X-Access-Code"}

VALID = [PROJECT, MINIMAL, changed(MINIMAL, methods={"core/loki": {"pageSize": 500}}),
         changed(MINIMAL, target={"baseUrl": "https://example.test"}),
         changed(MINIMAL, accounts={"roles": STATIC_ROLES, "login": STATIC_HEADER}),
         changed(MINIMAL, accounts={"roles": STATIC_ROLES, "login": {
             **STATIC_HEADER, "verify": {"endpoint": "/api/login", "bodyTemplate": {"code": "{password}"}}}}),
         changed(PROJECT, accounts={**PROJECT["accounts"], "login": {
             **PROJECT["accounts"]["login"], "kind": "token-endpoint"}})]

INVALID = [
    (without(PROJECT, "project"), "$"),
    (changed(PROJECT, target={"healthcheck": "/api/health"}), "$.target"),
    (changed(PROJECT, accounts={"roles": {"Company": {"keychain": "tightrein.sample.company"}}}), "$.accounts"),
    (changed(PROJECT, accounts={**PROJECT["accounts"], "roles": {"anonymous": {"keychain": "x"}}}),
     "$.accounts.roles"),
    (changed(MINIMAL, accounts={"roles": STATIC_ROLES, "login": {"kind": "static-header"}}), "$.accounts.login"),
    (changed(MINIMAL, accounts={"roles": STATIC_ROLES, "login": {**STATIC_HEADER, "tokenPath": "data.token"}}),
     "$.accounts.login.tokenPath"),
    (changed(PROJECT, accounts={**PROJECT["accounts"], "login": {**PROJECT["accounts"]["login"], "header": "X"}}),
     "$.accounts.login.header"),
    (changed(MINIMAL, accounts={"roles": STATIC_ROLES, "login": {**STATIC_HEADER, "header": "X Code"}}),
     "$.accounts.login.header"),
    (changed(MINIMAL, accounts={"roles": STATIC_ROLES, "login": {**STATIC_HEADER, "verify": {"endpoint": "/x"}}}),
     "$.accounts.login.verify"),
    (changed(PROJECT, evaluation={"budgetUsd": 20, "repeats": 2}), "$.evaluation.repeats"),
    (changed(PROJECT, stages={"deploy": {"limits": {"maxTurns": 3}}}), "$.stages"),
    (changed(PROJECT, models={"opus": {"model": "claude-opus"}}), "$.models.opus"),
    (changed(PROJECT, models={"opus": {"tool": "claude", "inputUsdPerMTok": 1}}), "$.models.opus"),
    (changed(PROJECT, routes={"default": 1}), "$.routes.default"),
    (changed(PROJECT, thresholds=changed(PROJECT["thresholds"], reproduceAttempts=2)),
     "$.thresholds.reproduceAttempts"),
    (changed(PROJECT, thresholds=changed(PROJECT["thresholds"], reproduceAttempts={"value": 2})),
     "$.thresholds.reproduceAttempts"),
    (changed(PROJECT, thresholds=changed(PROJECT["thresholds"], outageRatio=tunable(0.5, 0.3, 0.8))),
     "$.thresholds"),
    (changed(PROJECT, thresholds=changed(PROJECT["thresholds"], unknownLimit=tunable(1, 0, 2))), "$.thresholds"),
    (changed(PROJECT, schedule=changed(PROJECT["schedule"], weekly={"at": "8:30"})), "$.schedule.weekly.at"),
    (changed(PROJECT, localRun=changed(PROJECT["localRun"], backend={"command": "dotnet run"})), "$.localRun"),
    (changed(PROJECT, localRun={"ports": {"api": 70000}}), "$.localRun.ports.api"),
    (changed(PROJECT, checks={"commands": [{"name": "unit", "cwd": ".", "command": "make unit",
                                            "affected": {"map": [{"pattern": "x", "test": "y"}],
                                                         "command": "make unit"}}]}),
     "$.checks.commands[0].affected.command"),
    (changed(PROJECT, checks={"selfCheck": {"residuePatterns": []}}), "$.checks"),
    (changed(PROJECT, review={"deepTriggers": {"categories": ["ui"]}}), "$.review.deepTriggers.categories[0]"),
    (changed(PROJECT, stages={"fix": {"review": {"deep": {"tool": "codex"}}}}), "$.stages.fix.review.deep"),
    (changed(PROJECT, sources={"api-fuzz": {"specExport": {"command": "x"}}}), "$.sources['api-fuzz']"),
    (changed(PROJECT, sources={"platform-errors": {"levels": ["fail"]}}), "$.sources['platform-errors'].levels[0]"),
    (changed(PROJECT, sources={"server-log": {"levels": ["error"]}}), "$.sources"),
    (changed(PROJECT, sources={"e2e": {"locale": "zh-CN"}}), "$.sources"),
    (changed(PROJECT, sources={"alerts": {"every": "1w"}}), "$.sources.alerts.every"),
    (changed(PROJECT, sources={"api-fuzz": {"checks": {"responseSchema": "maybe"}}}),
     "$.sources['api-fuzz'].checks.responseSchema"),
    (changed(PROJECT, sources={"project-probes": [{"name": "Daily", "command": ["x"], "every": "1d"}]}),
     "$.sources['project-probes'][0].name"),
    (changed(PROJECT, extensions={"log-source": {"use": "core/local-dir"}}), "$.extensions"),
    (changed(PROJECT, sources={"static": {"semgrep": {"configs": []}}}), "$.sources.static.semgrep.configs"),
    (changed(PROJECT, stacks=["Asp.NetCore"]), "$.stacks[0]"),
    (changed(PROJECT, stacks="webstack"), "$.stacks"),
    (changed(PROJECT, extensions={"spec-export": {"use": "openapi-file"}}), "$.extensions['spec-export'].use"),
    (changed(PROJECT, methods={"loki": {}}), "$.methods"),
    (changed(PROJECT, methods={"core/loki": "gbk"}), "$.methods['core/loki']"),
    (changed(PROJECT, extensions={"api-docs": {"command": ["x"]}}), "$.extensions"),
    (changed(PROJECT, extensions={"authz-roles": {"command": ["x"], "mode": "extend"}}),
     "$.extensions['authz-roles'].mode"),
    (changed(PROJECT, extensions={"log-parse": {"mode": "extend"}}), "$.extensions['log-parse']"),
    (changed(PROJECT, extensions={"local-run": {"command": []}}), "$.extensions['local-run'].command"),
    (changed(PROJECT, extensions={"local-run": {"command": ["x"], "timeoutSeconds": 0}}),
     "$.extensions['local-run'].timeoutSeconds"),
]


@pytest.mark.parametrize("instance", VALID)
def test_valid_samples(instance):
    assert paths(CONFIG, instance) == set()


@pytest.mark.parametrize("instance,path", INVALID)
def test_invalid_samples(instance, path):
    assert path in paths(CONFIG, instance)


def test_required_keys_are_reported_with_their_location():
    errors = validate.validate(CONFIG, changed(MINIMAL, target={"healthcheck": "/health"}))
    assert [str(error) for error in errors] == ["$.target: 'baseUrl' is a required property"]


def test_every_extension_point_can_be_declared():
    declared = validate.schema(CONFIG)["properties"]["extensions"]["properties"]
    assert list(declared) == validate.schema("common.schema.json")["$defs"]["ExtensionPoint"]["enum"]
