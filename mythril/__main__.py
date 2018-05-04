#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""mythril.py: Bug hunting on the Ethereum blockchain

   http://www.github.com/b-mueller/mythril
"""


import logging
import json
import sys
import argparse
import os
import re

from ethereum import utils
from solc.exceptions import SolcError
import solc

from mythril.ether import util
from mythril.ether.contractstorage import get_persistent_storage
from mythril.ether.ethcontract import ETHContract
from mythril.ether.soliditycontract import SolidityContract
from mythril.rpc.client import EthJsonRpc
from mythril.ipc.client import EthIpc
from mythril.rpc.exceptions import ConnectionError
from mythril.support import signatures
from mythril.support.truffle import analyze_truffle_project
from mythril.support.loader import DynLoader
from mythril.exceptions import CompilerError, NoContractFoundError
from mythril.analysis.symbolic import SymExecWrapper
from mythril.analysis.callgraph import generate_graph
from mythril.analysis.traceexplore import get_serializable_statespace
from mythril.analysis.security import fire_lasers
from mythril.analysis.report import Report
from mythril.leveldb.client import EthLevelDB

# logging.basicConfig(level=logging.DEBUG)


def searchCallback(code_hash, code, addresses, balances):
    print("Matched contract with code hash " + code_hash)

    for i in range(0, len(addresses)):
        print("Address: " + addresses[i] + ", balance: " + str(balances[i]))


def exitWithError(format, message):
    if format == 'text' or format == 'markdown':
        print(message)
    else:
        result = {'success': False, 'error': str(message), 'issues': []}
        print(json.dumps(result))
    sys.exit()

def main():
    parser = argparse.ArgumentParser(description='Security analysis of Ethereum smart contracts')
    parser.add_argument("solidity_file", nargs='*')

    commands = parser.add_argument_group('commands')
    commands.add_argument('-g', '--graph', help='generate a control flow graph', metavar='OUTPUT_FILE')
    commands.add_argument('-x', '--fire-lasers', action='store_true', help='detect vulnerabilities, use with -c, -a or solidity file(s)')
    commands.add_argument('-t', '--truffle', action='store_true', help='analyze a truffle project (run from project dir)')
    commands.add_argument('-d', '--disassemble', action='store_true', help='print disassembly')
    commands.add_argument('-j', '--statespace-json', help='dumps the statespace json', metavar='OUTPUT_FILE')

    inputs = parser.add_argument_group('input arguments')
    inputs.add_argument('-c', '--code', help='hex-encoded bytecode string ("6060604052...")', metavar='BYTECODE')
    inputs.add_argument('-a', '--address', help='pull contract from the blockchain', metavar='CONTRACT_ADDRESS')
    inputs.add_argument('-l', '--dynld', action='store_true', help='auto-load dependencies from the blockchain')

    outputs = parser.add_argument_group('output formats')
    outputs.add_argument('-o', '--outform', choices=['text', 'markdown', 'json'], default='text', help='report output format', metavar='<text/json>')
    outputs.add_argument('--verbose-report', action='store_true', help='Include debugging information in report')

    database = parser.add_argument_group('local contracts database')
    database.add_argument('--init-db', action='store_true', help='initialize the contract database')
    database.add_argument('-s', '--search', help='search the contract database', metavar='EXPRESSION')

    utilities = parser.add_argument_group('utilities')
    utilities.add_argument('--hash', help='calculate function signature hash', metavar='SIGNATURE')
    utilities.add_argument('--storage', help='read state variables from storage index, use with -a', metavar='INDEX,NUM_SLOTS,[array] / mapping,INDEX,[KEY1, KEY2...]')
    utilities.add_argument('--solv', help='specify solidity compiler version. If not present, will try to install it (Experimental)', metavar='SOLV')

    options = parser.add_argument_group('options')
    options.add_argument('-m', '--modules', help='Comma-separated list of security analysis modules', metavar='MODULES')
    options.add_argument('--max-depth', type=int, default=12, help='Maximum recursion depth for symbolic execution')
    options.add_argument('--solc-args', help='Extra arguments for solc')
    options.add_argument('--phrack', action='store_true', help='Phrack-style call graph')
    options.add_argument('--enable-physics', action='store_true', help='enable graph physics simulation')
    options.add_argument('-v', type=int, help='log level (0-2)', metavar='LOG_LEVEL')
    options.add_argument('--leveldb', help='enable direct leveldb access operations', metavar='LEVELDB_PATH')

    rpc = parser.add_argument_group('RPC options')
    rpc.add_argument('-i', action='store_true', help='Preset: Infura Node service (Mainnet)')
    rpc.add_argument('--rpc', help='custom RPC settings', metavar='HOST:PORT / ganache / infura-[network_name]')
    rpc.add_argument('--rpctls', type=bool, default=False, help='RPC connection over TLS')
    rpc.add_argument('--ipc', action='store_true', help='Connect via local IPC')

    # Get config values

    args = parser.parse_args()

    try:
        mythril_dir = os.environ['MYTHRIL_DIR']
    except KeyError:
        mythril_dir = os.path.join(os.path.expanduser('~'), ".mythril")

    # Detect unsupported combinations of command line args

    if args.dynld and not args.address:
        exitWithError(args.outform, "Dynamic loader can be used in on-chain analysis mode only (-a).")

    # Initialize data directory and signature database

    if not os.path.exists(mythril_dir):
        logging.info("Creating mythril data directory")
        os.mkdir(mythril_dir)

    # If no function signature file exists, create it. Function signatures from Solidity source code are added automatically.

    signatures_file = os.path.join(mythril_dir, 'signatures.json')

    sigs = {}
    if not os.path.exists(signatures_file):
        logging.info("No signature database found. Creating empty database: " + signatures_file + "\n" +
                     "Consider replacing it with the pre-initialized database at https://raw.githubusercontent.com/ConsenSys/mythril/master/signatures.json")
        with open(signatures_file, 'a') as f:
            json.dump({}, f)

    with open(signatures_file) as f:
        try:
            sigs = json.load(f)
        except JSONDecodeError as e:
            exitWithError(args.outform, "Invalid JSON in signatures file " + signatures_file + "\n" + str(e))

    # Parse cmdline args

    if not (args.search or args.init_db or args.hash or args.disassemble or args.graph or args.fire_lasers or args.storage or args.truffle or args.statespace_json):
        parser.print_help()
        sys.exit()

    if args.v:
        if 0 <= args.v < 3:
            logging.basicConfig(level=[logging.NOTSET, logging.INFO, logging.DEBUG][args.v])
        else:
            exitWithError(args.outform, "Invalid -v value, you can find valid values in usage")

    if args.hash:
        print("0x" + utils.sha3(args.hash)[:4].hex())
        sys.exit()

    if args.truffle:
        try:
            analyze_truffle_project(args)
        except FileNotFoundError:
            print("Build directory not found. Make sure that you start the analysis from the project root, and that 'truffle compile' has executed successfully.")
        sys.exit()

    # Figure out solc binary and version
    # Only proper versions are supported. No nightlies, commits etc (such as available in remix)

    if args.solv:
        version = args.solv
        # tried converting input to semver, seemed not necessary so just slicing for now
        if version == str(solc.main.get_solc_version())[:6]:
            logging.info('Given version matches installed version')
            try:
                solc_binary = os.environ['SOLC']
            except KeyError:
                solc_binary = 'solc'
        else:
            if util.solc_exists(version):
                logging.info('Given version is already installed')
            else:
                try:
                    solc.install_solc('v' + version)
                except SolcError:
                    exitWithError(args.outform, "There was an error when trying to install the specified solc version")

            solc_binary = os.path.join(os.environ['HOME'], ".py-solc/solc-v" + version, "bin/solc")
            logging.info("Setting the compiler to " + str(solc_binary))
    else:
        try:
            solc_binary = os.environ['SOLC']
        except KeyError:
            solc_binary = 'solc'

    # Open LevelDB if specified

    if args.leveldb:
        ethDB = EthLevelDB(args.leveldb)
        eth = ethDB

    # Establish RPC/IPC connection if necessary

    if (args.address or args.init_db) and not args.leveldb:

        if args.i:
            eth = EthJsonRpc('mainnet.infura.io', 443, True)
            logging.info("Using INFURA for RPC queries")
        elif args.rpc:

            if args.rpc == 'ganache':
                rpcconfig = ('localhost', 7545, False)

            else:

                m = re.match(r'infura-(.*)', args.rpc)

                if m and m.group(1) in ['mainnet', 'rinkeby', 'kovan', 'ropsten']:
                    rpcconfig = (m.group(1) + '.infura.io', 443, True)

                else:
                    try:
                        host, port = args.rpc.split(":")
                        rpcconfig = (host, int(port), args.rpctls)

                    except ValueError:
                        exitWithError(args.outform, "Invalid RPC argument, use 'ganache', 'infura-[network]' or 'HOST:PORT'")

            if (rpcconfig):

                eth = EthJsonRpc(rpcconfig[0], int(rpcconfig[1]), rpcconfig[2])
                logging.info("Using RPC settings: %s" % str(rpcconfig))

            else:
                exitWithError(args.outform, "Invalid RPC settings, check help for details.")

        elif args.ipc:
            try:
                eth = EthIpc()
            except Exception as e:
                exitWithError(args.outform, "IPC initialization failed. Please verify that your local Ethereum node is running, or use the -i flag to connect to INFURA. \n" + str(e))

        else:  # Default configuration if neither RPC or IPC are set

            eth = EthJsonRpc('localhost', 8545)
            logging.info("Using default RPC settings: http://localhost:8545")


    # Database search ops

    if args.search or args.init_db:
        contract_storage, _ = get_persistent_storage(mythril_dir)
        if args.search:
            try:
                if not args.leveldb:
                    contract_storage.search(args.search, searchCallback)
                else:
                    ethDB.search(args.search, searchCallback)
            except SyntaxError:
                exitWithError(args.outform, "Syntax error in search expression.")
        elif args.init_db:
            try:
                contract_storage.initialize(eth)
            except FileNotFoundError as e:
                exitWithError(args.outform, "Error syncing database over IPC: " + str(e))
            except ConnectionError as e:
                exitWithError(args.outform, "Could not connect to RPC server. Make sure that your node is running and that RPC parameters are set correctly.")

        sys.exit()

    # Load / compile input contracts

    contracts = []
    address = None

    if args.code:
        address = util.get_indexed_address(0)
        contracts.append(ETHContract(args.code, name="MAIN"))

    # Get bytecode from a contract address

    elif args.address:
        address = args.address
        if not re.match(r'0x[a-fA-F0-9]{40}', args.address):
            exitWithError(args.outform, "Invalid contract address. Expected format is '0x...'.")

        try:
            code = eth.eth_getCode(args.address)
        except FileNotFoundError as e:
            exitWithError(args.outform, "IPC error: " + str(e))
        except ConnectionError as e:
            exitWithError(args.outform, "Could not connect to RPC server. Make sure that your node is running and that RPC parameters are set correctly.")
        except Exception as e:
            exitWithError(args.outform, "IPC / RPC error: " + str(e))
        else:
            if code == "0x" or code == "0x0":
                exitWithError(args.outform, "Received an empty response from eth_getCode. Check the contract address and verify that you are on the correct chain.")
            else:
                contracts.append(ETHContract(code, name=args.address))

    # Compile Solidity source file(s)

    elif args.solidity_file:
        address = util.get_indexed_address(0)
        if args.graph and len(args.solidity_file) > 1:
            exitWithError(args.outform, "Cannot generate call graphs from multiple input files. Please do it one at a time.")

        for file in args.solidity_file:
            if ":" in file:
                file, contract_name = file.split(":")
            else:
                contract_name = None

            file = os.path.expanduser(file)

            try:
                signatures.add_signatures_from_file(file, sigs)
                contract = SolidityContract(file, contract_name, solc_args=args.solc_args)
                logging.info("Analyzing contract %s:%s" % (file, contract.name))
            except FileNotFoundError:
                exitWithError(args.outform, "Input file not found: " + file)
            except CompilerError as e:
                exitWithError(args.outform, e)
            except NoContractFoundError:
                logging.info("The file " + file + " does not contain a compilable contract.")
            else:
                contracts.append(contract)

        # Save updated function signatures
        with open(signatures_file, 'w') as f:
            json.dump(sigs, f)

    else:
        exitWithError(args.outform, "No input bytecode. Please provide EVM code via -c BYTECODE, -a ADDRESS, or -i SOLIDITY_FILES")

    # Commands

    if args.storage:
        if not args.address:
            exitWithError(args.outform, "To read storage, provide the address of a deployed contract with the -a option.")
        else:
            (position, length, mappings) = (0, 1, [])
            try:
                params = args.storage.split(",")
                if params[0] == "mapping":
                    if len(params) < 3:
                        exitWithError(args.outform, "Invalid number of parameters.")
                    position = int(params[1])
                    position_formatted = utils.zpad(utils.int_to_big_endian(position), 32)
                    for i in range(2, len(params)):
                        key = bytes(params[i], 'utf8')
                        key_formatted = utils.rzpad(key, 32)
                        mappings.append(int.from_bytes(utils.sha3(key_formatted + position_formatted), byteorder='big'))

                    length = len(mappings)
                    if length == 1:
                        position = mappings[0]

                else:
                    if len(params) >= 4:
                        exitWithError(args.outform, "Invalid number of parameters.")

                    if len(params) >= 1:
                        position = int(params[0])
                    if len(params) >= 2:
                        length = int(params[1])
                    if len(params) == 3 and params[2] == "array":
                        position_formatted = utils.zpad(utils.int_to_big_endian(position), 32)
                        position = int.from_bytes(utils.sha3(position_formatted), byteorder='big')

            except ValueError:
                exitWithError(args.outform, "Invalid storage index. Please provide a numeric value.")

            try:
                if length == 1:
                    print("{}: {}".format(position, eth.eth_getStorageAt(args.address, position)))
                else:
                    if len(mappings) > 0:
                        for i in range(0, len(mappings)):
                            position = mappings[i]
                            print("{}: {}".format(hex(position), eth.eth_getStorageAt(args.address, position)))
                    else:
                        for i in range(position, position + length):
                            print("{}: {}".format(hex(i), eth.eth_getStorageAt(args.address, i)))
            except FileNotFoundError as e:
                exitWithError(args.outform, "IPC error: " + str(e))
            except ConnectionError as e:
                exitWithError(args.outform, "Could not connect to RPC server. Make sure that your node is running and that RPC parameters are set correctly.")


    elif args.disassemble:
        easm_text = contracts[0].get_easm()
        sys.stdout.write(easm_text)


    elif args.graph or args.fire_lasers:
        if not contracts:
            exitWithError(args.outform, "input files do not contain any valid contracts")

        if args.graph:
            if args.dynld:
                sym = SymExecWrapper(contracts[0], address, dynloader=DynLoader(eth), max_depth=args.max_depth)
            else:
                sym = SymExecWrapper(contracts[0], address, max_depth=args.max_depth)

            html = generate_graph(sym, physics=args.enable_physics, phrackify=args.phrack)

            try:
                with open(args.graph, "w") as f:
                    f.write(html)
            except Exception as e:
                exitWithError(args.outform, "Error saving graph: " + str(e))

        else:
            all_issues = []
            for contract in contracts:
                if args.dynld:
                    sym = SymExecWrapper(contract, address, dynloader=DynLoader(eth), max_depth=args.max_depth)
                else:
                    sym = SymExecWrapper(contract, address, max_depth=args.max_depth)

                if args.modules:
                    issues = fire_lasers(sym, args.modules.split(","))
                else:
                    issues = fire_lasers(sym)

                if type(contract) == SolidityContract:
                    for issue in issues:
                        issue.add_code_info(contract)

                all_issues += issues

            # Finally, output the results
            report = Report(args.verbose_report)
            for issue in all_issues:
                report.append_issue(issue)

            outputs = {
                'json': report.as_json(),
                'text': report.as_text() or "The analysis was completed successfully. No issues were detected.",
                'markdown': report.as_markdown() or "The analysis was completed successfully. No issues were detected."
            }
            print(outputs[args.outform])

    elif args.statespace_json:
        if not contracts:
            exitWithError(args.outform, "input files do not contain any valid contracts")

        if args.dynld:
            sym = SymExecWrapper(contracts[0], address, dynloader=DynLoader(eth), max_depth=args.max_depth)
        else:
            sym = SymExecWrapper(contracts[0], address, max_depth=args.max_depth)

        try:
            with open(args.statespace_json, "w") as f:
                json.dump(get_serializable_statespace(sym), f)
        except Exception as e:
            exitWithError(args.outform, "Error saving json: " + str(e))

    else:
        parser.print_help()

if __name__ == "__main__":
    main()

