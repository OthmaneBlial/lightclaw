from scripts.check_launch_pack import validate_launch_pack


def test_launch_pack_is_complete_without_false_external_claims():
    assert validate_launch_pack() == []

