#!/usr/bin/env python3

# A Websockets client talks to nanocast server
# https://github.com/nano-wallet-company/nano-wallet-server

# Author: twitter.com/alpacatunnel


import os
import json
import asyncio
from aiohttp import WSMsgType
from typing import List, Dict

from .ws_helper import ws_connect, ws_recv, ws_send
from .log import print_log
from .nano_account import Account

EMPTY_PREVIOUS = '0000000000000000000000000000000000000000000000000000000000000000'
LIGHT_SERVER = 'https://light.nano.org/'
# LIGHT_SERVER = 'https://10.1.1.31'


class NanoClientError(Exception):
    pass


class NanocastClient():

    def __init__(self, server=LIGHT_SERVER):
        self.server = server
        self.ws = None

    async def connect(self, verify_ssl=True):
        # header of Android and iOS
        headers = {
            'X-Client-Version': '30',
            'User-Agent': 'SwiftWebSocket'
        }
        ws, session = await ws_connect(self.server, verify_ssl=verify_ssl, headers=headers)
        if not ws:
            raise NanoClientError('connect to server failed: {}'.format(self.server))
        self.ws = ws
        self._ws_session = session  # keep the session, otherwise it will be closed

    async def close(self):
        if self.ws:
            await self._ws_session.close()
            await self.ws.close()

    def __del__(self):
        asyncio.ensure_future(self.close())

    async def _ws_send(self, request_dict):
        while self.ws is None:
            await asyncio.sleep(0.01)
        data = json.dumps(request_dict)
        return await ws_send(self.ws, data, WSMsgType.TEXT)

    async def _ws_recv(self):
        while self.ws is None:
            await asyncio.sleep(0.01)
        msg = await ws_recv(self.ws)
        if msg.type == WSMsgType.TEXT:
            try:
                return json.loads(msg.data)
            except:
                print_log('Load json string failed: {}'.format(msg.data))
                return {}
        else:
            print_log('Got unexpected message type: {}'.format(msg.type))
            return {}

    async def _ws_recv_until_success(self, excepted_keys: List[str]) -> Dict:
        """
        Receive until get the excepted_keys in the response dict.

        The nanocast server has these features:
        1) did not implement multiplexing channels over websockets,
        2) handle requests and send responses asynchronously,
        3) broadcast price data periodically.

        So if a client sends requests too quickly, it's difficult to find out
        which response belongs to which request.
        (Client sends request A, then B, but server may response B, then A.)

        To solve this, we must slow down the requests, and don't send a request until
        a previous response is received. And we must specially handle price messages.
        """

        if 'currency' in excepted_keys and 'price' in excepted_keys:
            ignore_price = False
        else:
            ignore_price = True

        # price data broadcast interval is 60s, so work acturally timeout after 90 * 10
        retry_times = 10
        if 'work' in excepted_keys:
            timeout = 90
        else:
            timeout = 30

        error = 'Failed to get the expect data from server'
        for _x in range(retry_times):
            future = self._ws_recv()
            try:
                response_dict = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                error = 'Timeout receiving websockets message'
                break

            # may be binary data or malformed json
            if not response_dict:
                continue

            # If ignore broadcasted price message here,
            # must send a separate price_data request to fetch it.
            if ignore_price and 'currency' in response_dict and 'price' in response_dict:
                print_log('Got periodically data message')
                continue

            if 'error' in response_dict:
                error = 'Got error from server: {}'.format(response_dict['error'])
                break

            expected = True
            for key in excepted_keys:
                if key not in response_dict:
                    expected = False
                    break

            if not expected:
                continue

            # return a fully expected dict
            return response_dict

        print_log(error)
        raise NanoClientError(error)

    async def _ws_request(self, request_dict, excepted_keys):
        await self._ws_send(request_dict)
        await asyncio.sleep(0.03)
        response_dict = await self._ws_recv_until_success(excepted_keys)
        return response_dict

    async def price_data(self):
        request_dict = {
            'action': 'price_data',
            'currency': 'usd'
        }
        excepted_keys = ['currency', 'price']
        return await self._ws_request(request_dict, excepted_keys)

    async def work_generate(self, hash):
        request_dict = {
            'action': 'work_generate',
            'hash': hash
        }
        excepted_keys = ['work']
        return await self._ws_request(request_dict, excepted_keys)

    async def account_balance(self, account):
        request_dict = {
            'action': 'account_balance',
            'account': account
        }
        excepted_keys = ['balance', 'pending']
        return await self._ws_request(request_dict, excepted_keys)

    async def account_info(self, account):
        request_dict = {
            'action': 'account_info',
            'representative': True,
            'pending': True,
            'account': account
        }
        excepted_keys = ['balance', 'pending', 'frontier']
        return await self._ws_request(request_dict, excepted_keys)

    async def pending(self, account):
        request_dict = {
            'action': 'pending',
            'count': 10,
            'account': account
        }
        excepted_keys = ['blocks']
        response_dict = await self._ws_request(request_dict, excepted_keys)
        return response_dict['blocks']

    async def account_history(self, account, count=10, head=None):
        if head:
            request_dict = {
                'action': 'account_history',
                'raw': True,
                'account': account,
                'count': count,
                'head': head
            }
        else:
            request_dict = {
                'action': 'account_history',
                'raw': True,
                'account': account,
                'count': count
            }
        excepted_keys = ['account', 'history']
        response_dict = await self._ws_request(request_dict, excepted_keys)
        return response_dict['history']

    async def block(self, hash):
        """
        Use self.block_info() instead, it returns the amount.
        """
        request_dict = {
            'action': 'block',
            'hash': hash
        }
        excepted_keys = ['contents']
        response_dict = await self._ws_request(request_dict, excepted_keys)
        contents = response_dict['contents']
        return json.loads(contents)

    async def block_info(self, hash):
        request_dict = {
            'action': 'blocks_info',
            'hashes': [hash]
        }
        excepted_keys = ['blocks']
        response_dict = await self._ws_request(request_dict, excepted_keys)
        _block_info = response_dict['blocks'][hash]
        amount = _block_info.get('amount')
        contents = json.loads(_block_info['contents'])
        contents['amount'] = amount
        return contents

    async def block_hash(self, account, previous, representative, balance, link):
        """
        Only support "state" block, because other block types are obsoleted.
        """

        if not previous:
            previous = EMPTY_PREVIOUS
        if not representative:
            representative = 'xrb_1nanode8ngaakzbck8smq6ru9bethqwyehomf79sae1k7xd47dkidjqzffeg' # Nanode Rep

        block_dict = {
            'type': 'state',
            'account': account,
            'previous': previous,
            'representative': representative,
            'balance': balance,
            'link': link,
        }
        block_json = json.dumps(block_dict)

        request_dict = {
            'action': 'block_hash',
            'block': block_json
        }
        excepted_keys = ['hash']
        return await self._ws_request(request_dict, excepted_keys)

    async def process(self, account, previous, representative, balance, link, signature, work):
        """
        Only support "state" block, because other block types are obsoleted.
        """

        if not previous:
            previous = EMPTY_PREVIOUS
        if not representative:
            representative = 'xrb_1nanode8ngaakzbck8smq6ru9bethqwyehomf79sae1k7xd47dkidjqzffeg' # Nanode Rep

        block_dict = {
            'type': 'state',
            'account': account,
            'previous': previous,
            'representative': representative,
            'balance': balance,
            'link': link,
            'signature': signature,
            'work': work
        }
        block_json = json.dumps(block_dict)

        request_dict = {
            'action': 'process',
            'block': block_json
        }
        excepted_keys = ['hash']
        return await self._ws_request(request_dict, excepted_keys)


class NanoLightClient():

    def __init__(self, account: Account):
        self.account = account
        self.cast = NanocastClient()

    async def connect(self):
        await self.cast.connect()

    async def _process_state_block(self, previous, representative, amount, link):
        hash_dict = await self.block_hash(
            account=self.account.xrb_account,
            previous=previous,
            representative=representative,
            balance=amount,
            link=link
        )

        signature = self.account.sign(hash_dict['hash']).hex()

        if previous:
            work_dict = await self.work_generate(previous)
        else: # for open block
            work_dict = await self.work_generate(self.account.public_key.hex())

        response_dict = await self.process(
            account=self.account.xrb_account,
            previous=previous,
            representative=representative,
            balance=amount,
            link=link,
            signature=signature,
            work=work_dict['work']
        )

        return response_dict['hash']

    async def _get_sent_amount(self, source_hash):
        source_block = await self.block_info(source_hash)
        amount = source_block['amount']
        if not amount:
            raise Exception('Did not get the amount from source block hash')
        return int(amount)

    async def state(self):
        return await self.cast.account_info(self.account.xrb_account)

    async def history(self, count=10, head=None):
        return await self.cast.account_history(self.account.xrb_account, count=count, head=head)

    async def open(self, source_hash):
        """
        source_hash: Pairing Send Block's Hash, the 'link'
        """

        previous, representative = None, None
        amount = await self._get_sent_amount(source_hash)

        frontier_hash = await self._process_state_block(
            previous, representative, amount, source_hash)

        print_log('Received Nano: {} raw'.format(amount))
        print_log('Frontier block hash is: {}'.format(frontier_hash))

        return frontier_hash

    async def receive(self, source_hash):
        """
        source_hash: Pairing Send Block's Hash, the 'link'
        """

        try:
            info = await self.cast.account_info(self.account.xrb_account)
        except NanoClientError as e:
            if 'Account not found' in str(e):
                print_log('Account not opened yet, receive with a open block.')
                return await self.open(source_hash)
            else:
                raise

        balance_before = int(info['balance'])
        previous = info['frontier']
        representative = info['representative']

        if int(info['pending']) == 0:
            print_log('No pending Nano to receive.')
            return

        amount = await self._get_sent_amount(source_hash)
        balance_after = balance_before + amount

        print_log('Balance before    : {} raw'.format(balance_before))
        print_log('Amount from source: {} raw'.format(amount))
        print_log('Total pending Nano: {} raw'.format(info['pending']))

        frontier_hash = await self._process_state_block(
            previous, representative, balance_after, source_hash)

        print_log('Received Nano     : {} raw'.format(amount))
        print_log('Balance after     : {} raw'.format(balance_after))
        print_log('Frontier block hash is: {}'.format(frontier_hash))

        return frontier_hash

    async def receive_all(self):
        """
        Receive all pending block.
        """
        pending_blocks = await self.cast.pending(self.account.xrb_account)
        if not pending_blocks:
            print_log('No pending block found.')
            return

        print_log('pending_blocks: {}.'.format(pending_blocks))

        for block in pending_blocks:
            await self.receive(block)

    def _to_raw(self, amount):
        """
        Convert NANO (str/float/int) to raw.
        1 NANO = 10^30 raw
        """

        # convert to str first. For float, this will be rounded up and lost precision
        amount = str(amount)

        if '.' not in amount:
            amount += '.0'
        a, b = amount.split('.')
        b = b[0:30]
        b += '0' * (30 - len(b))

        return int(a) * 10**30 + int(b)

    async def send(self, dest_account, amount):
        amount = self._to_raw(amount)
        info = await self.cast.account_info(self.account.xrb_account)
        balance_before = int(info['balance'])
        previous = info['frontier']
        representative = info['representative']

        print_log('Balance before : {} raw'.format(balance_before))
        print_log('Amount to send : {} raw'.format(amount))

        if amount > balance_before:
            print_log('Can not send amount more than balance')
            return

        balance_after = balance_before - amount

        frontier_hash = await self._process_state_block(
            previous, representative, balance_after, dest_account)

        print_log('Balance after  : {} raw'.format(balance_after))
        print_log('Frontier block hash is: {}'.format(frontier_hash))

        return frontier_hash


async def get_price():
    client = NanocastClient()
    await client.connect(verify_ssl=False)

    price = await client.price_data()
    print_log(price)

    await client.close()


def main_test():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(get_price())


if __name__ == '__main__':
    main_test()