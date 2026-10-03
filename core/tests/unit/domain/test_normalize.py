from tightrein.domain.normalize import MAX_LENGTH, Rule, normalize


def test_guid():
    assert normalize("user 3f2a8b1c-1d2e-4f50-9a6b-7c8d9e0f1a2b missing", ()) == "user <guid> missing"


def test_iso_time_and_clock_time():
    assert normalize("at 2026-09-29T02:15:03Z and 02:15:03", ()) == "at <time> and <time>"


def test_hex_before_number():
    assert normalize("commit d6f37025 hash a1234567ff", ()) == "commit <hex> hash <hex>"


def test_long_number_but_not_short():
    assert normalize("order 123456 page 12", ()) == "order <num> page 12"


def test_windows_and_posix_paths():
    text = r"cannot open C:\APP\online_services\upload\a.dat or /tmp/x/y.log"
    assert normalize(text, ()) == "cannot open <path> or <path>"


def test_quoted_values():
    assert normalize("field 'abc' invalid, got \"xyz\"", ()) == "field <value> invalid, got <value>"


def test_project_rules_run_after_defaults():
    rules = (Rule(pattern=r"tenant-[a-z]+", replacement="<tenant>"),)
    assert normalize("tenant-alpha failed", rules) == "<tenant> failed"


def test_truncates_to_max_length():
    assert len(normalize("x" * 500, ())) == MAX_LENGTH == 200


def test_collapses_whitespace():
    assert normalize("  a \n\t b  ", ()) == "a b"


from tightrein.domain.normalize import location


def test_api_location_keeps_template_and_replaces_ids():
    assert location("post /api/Material/123?x=1") == "POST /api/Material/{id}"
    assert location("GET /api/Order/{id}") == "GET /api/Order/{id}"
    assert location("DELETE /api/User/3f2a8b1c-1d2e-4f50-9a6b-7c8d9e0f1a2b") == "DELETE /api/User/{id}"


def test_page_location_replaces_ids():
    assert location("/newhome/CompanyDetail/42?tab=a") == "/newhome/CompanyDetail/:id"
    assert location("/newhome/calculate") == "/newhome/calculate"


def test_code_location_drops_line_and_column():
    assert location("Services/MaterialService.cs:MaterialService.Query:118") == "Services/MaterialService.cs:MaterialService.Query"
    assert location("Services/MaterialService.cs:MaterialService.Query:118:7") == "Services/MaterialService.cs:MaterialService.Query"
    assert location("Services/MaterialService.cs:MaterialService.Query") == "Services/MaterialService.cs:MaterialService.Query"


def test_plain_category_is_kept():
    assert location("Microsoft.EntityFrameworkCore.Database.Command") == "Microsoft.EntityFrameworkCore.Database.Command"
