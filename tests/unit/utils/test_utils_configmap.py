import os

from piceli.k8s.utils import utils_configmap


def test_file_mappings() -> None:
    """test that values in some files are mapped correctly"""
    config_filename = "some_config.conf"
    data = utils_configmap.get_configmap_data_from_files(
        filepaths=[
            os.path.join("tests", "unit", "utils", "resources", config_filename)
        ],
        mappings={
            "HOST": "localhost",
            "PORT": "1234",
        },
    )
    assert config_filename in data
    parsed_file = data[config_filename]
    assert "{" not in parsed_file
    assert "}" not in parsed_file
    assert "HOST" not in parsed_file
    assert "PORT" not in parsed_file
    assert "localhost" in parsed_file
    assert "1234" in parsed_file
