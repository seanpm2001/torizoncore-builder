load 'bats/bats-support/load.bash'
load 'bats/bats-assert/load.bash'
load 'bats/bats-file/load.bash'

@test "images serve: check help output" {
    run torizoncore-builder images serve --help
    assert_success
    assert_output --partial 'Path to TorizonCore TEZI images directory.'
}

@test "images serve: check if 'image_list.json' file exists" {
    local IMAGE_DIR=$(mktemp -d tmpdir.XXXXXXXXXXXXXXXXXXXXXXXXX)

    run torizoncore-builder images serve $IMAGE_DIR
    assert_failure
    assert_output --regexp '^Error: The Toradex Easy Installer.*does not exist inside.*directory.$'

    rm -rf $IMAGE_DIR
}

@test "images serve: check zeroconf tezi service response." {
    requires-device

    IMAGE_DIR="samples/images"
    torizoncore-builder-bg images serve $IMAGE_DIR

    run avahi-browse-domains -a -t
    assert_success
    assert_output --partial '_tezi._tcp'
    assert_output --partial 'Custom Toradex Easy Installer Feed'

    stop-torizoncore-builder-bg
}

@test "images serve: check if 'image_list.json' is being served." {
    requires-device

    IMAGE_DIR="samples/images"
    torizoncore-builder-bg images serve $IMAGE_DIR

    run wget -S http://localhost/image_list.json -O -
    assert_success
    assert_output --partial 'Cache-Control: no-store,max-age=0'
    assert_output --partial 'config_format'
    assert_output --partial '/image.json'

    stop-torizoncore-builder-bg
}

