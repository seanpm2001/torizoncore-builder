"""
CLI handling for images subcommand
"""

import logging
import os
import shutil

from datetime import datetime, timezone

import dateutil.parser

from tcbuilder.backend import images, sotaops, common
from tcbuilder.errors import UserAbortError, InvalidStateError, InvalidDataError
from tcbuilder.backend.registryops import RegistryOperations

log = logging.getLogger("torizon." + __name__)

JSON_EXT = ".json"

IMAGES_DIR = "images/"
DIRECTOR_DIR = "metadata/director/"
IMAGEREPO_DIR = "metadata/image-repo/"
DOCKERMETA_DIR = "metadata/docker/"

OFFLINE_UPDATE_SNAPSHOT_FILE = "offline-update-snapshot.json"
DEFAULT_PLATFORMS = ["linux/arm/v7", "linux/arm64"]


def l1_pref(orgstr):
    """Add L1_PREF prefix to orgstr"""
    return "\n=>> " + orgstr


def get_extra_dirs(storage_dir, main_dirs):
    """
    Get all directories names inside "storage" that should be removed when
    unpacking a new TEZI image but that are not included in the list of
    "keep directories" and the list of "main directories". At this time,
    only the "toolchain directory" should be kept between images unpack.

    :param storage_dir: Storage directory.
    :param main_dirs: List of main directories for the unpacking.
    :returns: A list of extra directories that should be removed.
    """

    # Directories that should be kept between images "unpacks"
    keep_dirs = [os.path.join(storage_dir, "toolchain")]

    extra_dirs = []

    for dirname in os.listdir(storage_dir):
        abs_dirname = os.path.join(storage_dir, dirname)
        if abs_dirname not in keep_dirs + main_dirs:
            extra_dirs.append(abs_dirname)

    return extra_dirs


def prepare_storage(storage_directory, remove_storage):
    """ Prepare Storage directory for unpacking"""

    storage_dir = os.path.abspath(storage_directory)

    if not os.path.exists(storage_dir):
        os.mkdir(storage_dir)

    # Main directories: will be cleared and returned by this function.
    main_dirs = [os.path.join(storage_dir, dirname)
                 for dirname in ("tezi", "sysroot", "ostree-archive")]

    # Extra directories: will be cleared but not returned.
    extra_dirs = get_extra_dirs(storage_dir, main_dirs)

    all_dirs = main_dirs + extra_dirs
    need_clearing = False
    for src_dir in all_dirs:
        if os.path.exists(src_dir):
            need_clearing = True
            break

    if need_clearing and not remove_storage:
        # Let's ask the user about that:
        ans = input("Storage not empty. Delete current image before continuing? [y/N] ")
        if ans.lower() != "y":
            raise UserAbortError()

    for src_dir in all_dirs:
        if os.path.exists(src_dir):
            shutil.rmtree(src_dir)

    return main_dirs


def do_images_download(args):
    """Run 'images download' subcommand"""

    r_ip = common.resolve_remote_host(args.remote_host, args.mdns_source)
    dir_list = prepare_storage(args.storage_directory, args.remove_storage)
    images.download_tezi(r_ip, args.remote_username, args.remote_password,
                         args.remote_port,
                         dir_list[0], dir_list[1], dir_list[2])

def do_images_serve(args):
    """
    Wrapper for 'images serve' subcommand.
    """
    images.serve(args.images_directory)


def load_offupd_metadata(image_name, dest_dir=None):
    """Load and possibly save the metadata for the specified image name

    This function will save both the metadata directly related to the specified
    image and also the offline-update snapshot which lists all updates
    available.

    :param image_name: If a name without an extension of ".json" or a "/"
                       separator is passed, assume this is the name of an
                       image whose information is to be fetched from the OTA
                       server.
    :param dest_dir: If not None, the main metadata and the snapshot files will
                     be saved into the directory defined by this parameter.
    """

    # Load offline-update targets and snapshot metadata:
    if os.sep in image_name or image_name.endswith(JSON_EXT):
        # Treat image_name as a file name.
        if not image_name.endswith(JSON_EXT):
            image_name += JSON_EXT

        # Load targets metadata into memory.
        log.info(f"Loading offline-update targets metadata from {image_name}")
        offupd_targets_info = images.load_metadata(image_name)

        # Load snapshot metadata (search same directory as the targets metadata file is).
        offupd_snapshot_file = os.path.join(
            os.path.dirname(image_name), OFFLINE_UPDATE_SNAPSHOT_FILE)
        log.info(f"Loading offline-update snapshot metadata from {offupd_snapshot_file}")
        offupd_snapshot_info = images.load_metadata(offupd_snapshot_file)

        # Make a copy into the destination directory:
        if dest_dir is not None:
            shutil.copy(image_name, dest_dir)
            shutil.copy(offupd_snapshot_file, dest_dir)
    else:
        assert False, \
            "NOT IMPLEMENTED: Load offline-update targets metadata from OTA server"

    return offupd_targets_info, offupd_snapshot_info


def validate_offupd_metadata(offupd_targets_info, offupd_snapshot_info):
    """Perform validations on the offline-update metadata and its snapshot"""

    # Helper function:
    def ensure(cond, message):
        if not cond:
            raise InvalidDataError("Error: " + message)

    log.debug("Validating offline-update metadata")

    now = datetime.now(timezone.utc)

    # Basic check of the snapshot metadata alone.
    snapshot_meta = offupd_snapshot_info["parsed"]["signed"]

    ensure(snapshot_meta["_type"] == "Offline-Snapshot",
           "_type in snapshot metadata does not equal 'Offline-Snapshot'")
    ensure(dateutil.parser.parse(snapshot_meta["expires"]) > now,
           "Offline snapshot metadata is already expired")

    # Basic check of the targets metadata alone.
    targets_meta = offupd_targets_info["parsed"]["signed"]

    ensure(targets_meta["_type"] == "Offline-Targets",
           "_type in targets metadata does not equal 'Offline-Targets'")

    ensure(dateutil.parser.parse(targets_meta["expires"]) > now,
           "Offline targets metadata is already expired")

    # Cross-checks:
    targets_file = os.path.basename(offupd_targets_info["file"])
    ensure(targets_file in snapshot_meta["meta"],
           f"{targets_file} is not described in the snapshot metadata")

    ensure(snapshot_meta["meta"][targets_file]["hashes"]["sha256"] ==
           offupd_targets_info["sha256"],
           f"{targets_file} does not have the expected sha256")

    ensure(snapshot_meta["meta"][targets_file]["length"] ==
           offupd_targets_info["size"],
           f"{targets_file} does not have the expected size")

    ensure(snapshot_meta["meta"][targets_file]["version"] ==
           targets_meta["version"],
           f"{targets_file} does not have the expected version")

    # Maybe check signature (event though this is be done by the device) (TODO).
    log.info("Offline-update metadata passed basic validation")


def fetch_offupd_targets(
        targets_metadata, images_dir, docker_metadata_dir,
        ostree_url=None, repo_url=None, access_token=None,
        docker_platforms=None):
    """Fetch all targets referenced by the offline-update targets metadata

    :param targets_metadata: The targets metadata as fetched from the director
                             repository.
    :param images_dir: Directory where images would be stored.
    :param docker_metadata_dir: Directory where to store metadata for Docker.
    :param ostree_url: Base URL of the OSTree repository.
    :param repo_url: Base URL of the TUF repository as it appears in the
                     credentials file.
    :param access_token: OAuth2 access token giving access to the TUF repos of
                         the user at the OTA server.
    :param docker_platforms: List of platforms for fetching Docker images by
                             default.
    """

    validate = True
    tgtmeta = targets_metadata
    for tgtkey in targets_metadata:
        tgtformat = tgtmeta[tgtkey]["custom"]["targetFormat"]
        # Handle each type of target.
        if tgtformat == "OSTREE":
            params = {
                "target": tgtkey,
                "sha256": tgtmeta[tgtkey]["hashes"]["sha256"],
                "ostree_url": ostree_url,
                "images_dir": images_dir,
                "name": tgtmeta[tgtkey]["custom"]["name"],
                "version": tgtmeta[tgtkey]["custom"]["version"],
                "access_token": access_token
            }
            images.fetch_ostree_target(**params)

        elif tgtformat == "BINARY":
            params = {
                "target": tgtkey,
                "repo_url": repo_url,
                "images_dir": images_dir,
                "name": tgtmeta[tgtkey]["custom"]["name"],
                "version": tgtmeta[tgtkey]["custom"]["version"],
                "access_token": access_token
            }
            if validate:
                params.update({
                    "sha256": tgtmeta[tgtkey]["hashes"]["sha256"],
                    "length": tgtmeta[tgtkey]["length"],
                })

            # Handle compose and basic binary files differently:
            if "docker-compose" in tgtmeta[tgtkey]["custom"]["hardwareIds"]:
                params.update({
                    "req_platforms": docker_platforms,
                    "metadata_dir": docker_metadata_dir
                })
                images.fetch_compose_target(**params)
            else:
                images.fetch_binary_target(**params)

        else:
            assert False, \
                f"Do not know how to handle target of type {tgtformat}"


# pylint: disable=too-many-locals
def images_takeout(
        image_name, creds_file, output_dir,
        docker_logins=None, docker_platforms=None,
        force=False, validate=True):
    """Main handler for the 'images takeout' subcommand

    :param image_name: Name of the takeout image as defined at the OTA server
                       or the name a JSON file with the snapshot data for the
                       image.
    :param creds_file: Name of the `credentials.zip` file.
    :param output_dir: Directory where the takeout image will be created.
    :param docker_logins: A list-like object where one element is a pai
                          (username, password) to be used with the default
                          registry and the other items are 3-tuples
                          (registry, username, password) with authentication
                          information to be used with other registries.
    :param force: Whether to force the generation of the output directory.
    :param validate: Whether to validate the Uptane metadata.
    """

    # Create output directory or abort:
    if os.path.exists(output_dir):
        if force:
            log.debug(f"Removing existing output directory '{output_dir}'")
            shutil.rmtree(output_dir)
        else:
            raise InvalidStateError(
                f"Output directory '{output_dir}' already exists; please remove"
                " it or select another output directory.")

    os.makedirs(output_dir)

    # Build directory structure:
    images_dir = os.path.join(output_dir, IMAGES_DIR)
    director_dir = os.path.join(output_dir, DIRECTOR_DIR)
    imagerepo_dir = os.path.join(output_dir, IMAGEREPO_DIR)
    dockermeta_dir = os.path.join(output_dir, DOCKERMETA_DIR)

    os.makedirs(images_dir)
    os.makedirs(director_dir)
    os.makedirs(imagerepo_dir)
    os.makedirs(dockermeta_dir)

    try:
        # Configure Docker "operations" class.
        RegistryOperations.set_logins(docker_logins)

        # Load and validate top-level metadata (offline targets and snapshot):
        offupd_targets_info, offupd_snapshot_info = \
            load_offupd_metadata(image_name, dest_dir=director_dir)
        if validate:
            validate_offupd_metadata(offupd_targets_info, offupd_snapshot_info)

        # Load credentials file.
        server_creds = sotaops.ServerCredentials(creds_file)
        # log.debug(server_creds)

        # Get access token (this should be valid for hours).
        sota_token = sotaops.get_access_token(server_creds)

        # Fetch metadata from OTA server.
        log.info(l1_pref("Handling image-repository metadata"))
        images.fetch_imgrepo_metadata(
            server_creds.repo_url, imagerepo_dir, access_token=sota_token)

        log.info(l1_pref("Handling director-repository metadata"))
        # images.fetch_director_metadata()
        # log.debug(f"{metadata_dir} parameter not used YET!")

        # Fetch all targets specified in offline-update targets metadata:
        log.info(l1_pref("Handling Uptane targets"))
        fetch_offupd_targets(
            targets_metadata=offupd_targets_info["parsed"]["signed"]["targets"],
            ostree_url=server_creds.ostree_server,
            repo_url=server_creds.repo_url,
            images_dir=images_dir,
            access_token=sota_token,
            docker_metadata_dir=dockermeta_dir,
            docker_platforms=docker_platforms)

        common.set_output_ownership(output_dir, set_parents=True)

    except BaseException as exc:
        # Avoid leaving a damaged output around: we catch BaseException here
        # so that even keyboard interrupts are handled.
        if os.path.exists(output_dir):
            log.info(f"Removing output directory '{output_dir}' due to errors")
            shutil.rmtree(output_dir)
        raise exc
# pylint: enable=too-many-locals


def do_images_takeout(args):
    """Wrapper for 'images takeout' subcommand"""

    # Build list of logins:
    logins = []
    if args.main_login:
        logins.append(args.main_login)

    images_takeout(
        args.image_name, args.credentials, args.output_directory,
        docker_logins=logins,
        docker_platforms=(args.platforms or DEFAULT_PLATFORMS),
        force=args.force,
        validate=args.validate)


def images_unpack(image_dir, storage_dir, remove_storage=False):
    """Main handler for the 'images unpack' subcommand"""

    image_dir = os.path.abspath(image_dir)
    dir_list = prepare_storage(storage_dir, remove_storage)
    images.import_local_image(image_dir, dir_list[0], dir_list[1], dir_list[2])


def do_images_unpack(args):
    """Wrapper for 'images unpack' subcommand"""

    images_unpack(args.image_directory,
                  args.storage_directory,
                  args.remove_storage)


def init_parser(subparsers):
    """Initialize 'images' subcommands command line interface."""

    parser = subparsers.add_parser("images", help="Manage Toradex Easy Installer Images.")
    parser.add_argument("--remove-storage", dest="remove_storage", action="store_true",
                        help="""Automatically clear storage prior to unpacking a new Easy
                        Installer image.""")
    subparsers = parser.add_subparsers(title='Commands', required=True, dest='cmd')

    # images download
    subparser = subparsers.add_parser(
        "download",
        help="Download image from Toradex Artifactory and unpack it.")
    subparser.add_argument(
        "--remote-host", dest="remote_host",
        help="Hostname/IP address to target device.", required=True)
    common.add_ssh_arguments(subparser)
    subparser.add_argument(
        "--mdns-source", dest="mdns_source",
        help=("Use the given IP address as mDNS source. This is useful when "
              "multiple interfaces are used, and mDNS multicast requests are "
              "sent out the wrong network interface."))
    subparser.set_defaults(func=do_images_download)

    # images serve
    subparser = subparsers.add_parser(
        "serve",
        help="Serve TorizonCore TEZI images via HTTP.")
    subparser.add_argument(
        metavar="IMAGES_DIRECTORY",
        dest="images_directory",
        help="Path to TorizonCore TEZI images directory.")
    subparser.set_defaults(func=do_images_serve)

    # images takeout
    subparser = subparsers.add_parser(
        "takeout",
        help="Generate a takeout image to be used for offline updates.",
        epilog=("After the takeout image is generated, the output directory "
                "should be copied to the root directory of the removable media "
                "to be used for the offline updates of devices. Beware that the "
                "name of the directory with the takeout image as stored on the "
                "removable media MUST always be 'update' for it to be detected "
                "by the devices. Also, make sure that the filesystem on the "
                "removable media allows for long file names and is supported "
                "by TorizonCore."))
    subparser.add_argument(
        "image_name", metavar="TAKEOUT_IMAGE_NAME",
        help="Name of takeout image (as defined at the OTA server).")
    subparser.add_argument(
        "--credentials", dest="credentials",
        help="Relative path to credentials.zip.", required=True)
    subparser.add_argument(
        "--force", dest="force",
        default=False, action="store_true",
        help=("Force program output (remove output directory before "
              "generating the takeout image)."))
    subparser.add_argument(
        "--platform",
        action="append",
        metavar="PLATFORM",
        dest="platforms",
        help=("Define platform to select when not specified in the compose file "
              f"(can be specified multiple times; default: {', '.join(DEFAULT_PLATFORMS)})."))
    subparser.add_argument(
        "--login", nargs=2, dest="main_login",
        metavar=('USERNAME', 'PASSWORD'),
        help=("Request that the tool logs in to the default [Docker Hub] "
              "registry using specified USERNAME and PASSWORD."))
    subparser.add_argument(
        "--no-validate",
        dest="validate",
        help="Disable basic metadata validation (expiry date, number of targets, etc.).",
        action="store_false", default=True)
    # FIXME: Allow logging in also to other registries.
    subparser.add_argument(
        "--output-directory",
        help=("Relative path to the output directory (default: update/). If "
              "parent directories are passed such as in a/b/update/, they will "
              "be automatically created."),
        default="update/")
    subparser.set_defaults(func=do_images_takeout)

    # images unpack
    subparser = subparsers.add_parser(
        "unpack",
        help=("Unpack a specified Toradex Easy Installer image so it can be "
              "modified with the union subcommand."))
    subparser.add_argument(
        metavar="IMAGE", dest="image_directory", nargs='?',
        help="Path to Easy Installer file or directory.")

    subparser.set_defaults(func=do_images_unpack)
