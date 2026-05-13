import click

from . import __version__
from .developer import developer_cmd
from .install import install_cmd
from .integration import integration_cmd
from .tenant import tenant_cmd


@click.group()
@click.version_option(__version__)
def cli():
    """coo — multi-tenant COO agent platform CLI."""


@cli.group(name="platform")
def platform_grp():
    """Platform-layer operations."""


platform_grp.add_command(install_cmd)
cli.add_command(developer_cmd)
cli.add_command(tenant_cmd)
cli.add_command(integration_cmd)


if __name__ == "__main__":
    cli()
