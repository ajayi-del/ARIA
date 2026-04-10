# Prevent web3's broken pytest plugin (eth_typing incompatibility) from loading.
# web3/tools/pytest_ethereum requires ContractName which was removed in eth-typing 6.x.
collect_ignore_glob = []


def pytest_configure(config):
    # Unregister the web3 pytest plugin if it was auto-registered
    pluginmanager = config.pluginmanager
    try:
        pluginmanager.unregister(name="pytest-ethereum")
    except Exception:
        pass
