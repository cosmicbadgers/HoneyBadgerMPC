import random
from math import log
import asyncio
from itertools import islice
from honeybadgermpc.mpc import (
    Field, Poly, generate_test_triples, write_polys, TaskProgramRunner
)


sharedatadir = "sharedata"
triplesprefix = f'{sharedatadir}/test_triples'
oneminusoneprefix = f'{sharedatadir}/test_one_minusone'
filecount = 0


async def wait_for_preprocessing():
    while not os.path.exists(f"{sharedatadir}/READY"):
        print(f"waiting for preprocessing {sharedatadir}/READY")
        await asyncio.sleep(1)


async def multiplyShares(x, y, a, b, ab):
    D = (x - a).open()
    E = (y - b).open()
    xy = D*E + D*b + E*a + ab
    return await xy.open()


async def batchBeaver(ctx, Xs, Ys, As, Bs, ABs):
    Ds = await (Xs - As).open()  # noqa: W606
    Es = await (Ys - Bs).open()  # noqa: W606

    XYs = [await (ctx.Share(D*E) + D*b + E*a + ab).open() for (  # noqa: W606
        a, b, ab, D, E) in zip(As._shares, Bs._shares, ABs._shares, Ds, Es)]

    return XYs


async def batchSwitch(ctx, Xs, Ys, sbits, As, Bs, ABs, n):
    Ns = [1 / Field(2) for _ in range(n)]

    def toShareArray(arr):
        return ctx.ShareArray(list(map(ctx.Share, arr)))
    Xs, Ys, As, Bs, ABs, sbits = list(map(toShareArray, [Xs, Ys, As, Bs, ABs, sbits]))
    Ms = list(map(ctx.Share, await batchBeaver(ctx, sbits, (Xs - Ys), As, Bs, ABs)))

    t1s = [n * (x + y + m).v for x, y, m, n in zip(Xs._shares, Ys._shares, Ms, Ns)]
    t2s = [n * (x + y - m).v for x, y, m, n in zip(Xs._shares, Ys._shares, Ms, Ns)]
    return t1s, t2s


async def switch(ctx, a, b, sbit, x, y, xy):
    a, b, x, y, xy, sbit = map(ctx.Share, [a, b, x, y, xy, sbit])
    m = ctx.Share(await multiplyShares(sbit, (a - b), x, y, xy))
    n = 1 / Field(2)

    x = n * (a + b + m).v
    y = n * (a + b - m).v
    return x, y


def writeToFile(shares, nodeid):
    global filecount
    with open(f"{sharedatadir}/butterfly_online_{filecount}_{nodeid}.share", "w") as f:
        for share in shares:
            print(share.value, file=f)
    filecount += 1


def getTriplesAndSbit(tripleshares, randshares):
    a, b, ab = next(tripleshares).v, next(tripleshares).v, next(tripleshares).v
    sbit = next(randshares).v
    return a, b, ab, sbit


def getNTriplesAndSbits(tripleshares, randshares, n):
    As, Bs, ABs = [], [], []
    for _ in range(n):
        a, b, ab = list(islice(tripleshares, 3))
        As.append(a.v), Bs.append(b.v), ABs.append(ab.v)
    sbits = list(map(lambda x: x.v, list(islice(randshares, n))))
    return As, Bs, ABs, sbits


async def permutationNetwork(ctx, inputs, k, d, randshares, tripleshares, level=0):
    if level == int(log(k, 2)) - d:
        writeToFile(inputs, ctx.myid)
        return None

    if level > int(log(k, 2)) - d:
        return None

    if len(inputs) == 2:
        a, b, ab, sbit = getTriplesAndSbit(tripleshares, randshares)
        return await switch(ctx, inputs[0], inputs[1], sbit, a, b, ab)

    n = len(inputs)//2
    As, Bs, ABs, sbits = getNTriplesAndSbits(tripleshares, randshares, n)
    Xs = [inputs[2*i] for i in range(n)]
    Ys = [inputs[2*i + 1] for i in range(n)]
    layer1output1, layer1output2 = await batchSwitch(ctx, Xs, Ys, sbits, As, Bs, ABs, n)

    layer2output1 = await permutationNetwork(
        ctx, layer1output1, k, d, randshares, tripleshares, level+1
    )
    layer2output2 = await permutationNetwork(
        ctx, layer1output2, k, d, randshares, tripleshares, level+1
    )

    if layer2output1 is None or layer2output2 is None:
        return None

    As, Bs, ABs, sbits = getNTriplesAndSbits(tripleshares, randshares, n)
    result = await batchSwitch(ctx, layer2output1, layer2output2, sbits, As, Bs, ABs, n)
    result = [*sum(zip(result[0], result[1]), ())]

    return result


async def butterflyNetwork(ctx, **kwargs):
    k, delta, inputs = kwargs['k'], kwargs['delta'], kwargs['inputs']
    tripleshares = ctx.read_shares(open(f'{triplesprefix}-{ctx.myid}.share'))
    randshares = ctx.read_shares(open(f'{oneminusoneprefix}-{ctx.myid}.share'))
    print(f"[{ctx.myid}] Running permutation network.")
    shuffled = await permutationNetwork(
        ctx, inputs, k, delta, iter(randshares), iter(tripleshares)
    )
    if shuffled is not None:
        shuffledShares = list(map(ctx.Share, shuffled))
        openedValues = await asyncio.gather(*[s.open() for s in shuffledShares])
        print(f"[{ctx.myid}]", openedValues)
        return shuffledShares
    return None


def generate_random_shares(prefix, k, N, t):
    polys = [Poly.random(t, random.randint(0, 1)*2 - 1) for _ in range(k)]
    write_polys(prefix, Field.modulus, N, t, polys)


def runButterlyNetworkInTasks():
    N, t, k, delta = 3, 1, 128, 6
    inputs = [Field(i) for i in range(1, k+1)]

    print('Generating random shares of triples in sharedata/')
    generate_test_triples(triplesprefix, 1000, N, t)
    print('Generating random shares of 1/-1 in sharedata/')
    generate_random_shares(oneminusoneprefix, k * int(log(k, 2)), N, t)

    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()
    loop.set_debug(True)
    try:
        programRunner = TaskProgramRunner(N, t)
        programRunner.add(butterflyNetwork, k=k, delta=delta, inputs=inputs)
        loop.run_until_complete(programRunner.join())
    finally:
        loop.close()


if __name__ == "__main__":
    import sys
    import os
    from honeybadgermpc.config import load_config
    from honeybadgermpc.ipc import NodeDetails, ProcessProgramRunner
    from honeybadgermpc.exceptions import ConfigurationError

    configfile = os.environ.get('HBMPC_CONFIG')
    nodeid = os.environ.get('HBMPC_NODE_ID')
    runid = os.environ.get('HBMPC_RUN_ID')

    # override configfile if passed to command
    try:
        nodeid = sys.argv[1]
        configfile = sys.argv[2]
    except IndexError:
        pass

    if not nodeid:
        raise ConfigurationError('Environment variable `HBMPC_NODE_ID` must be set'
                                 ' or a node id must be given as first argument.')

    if not configfile:
        raise ConfigurationError('Environment variable `HBMPC_CONFIG` must be set'
                                 ' or a config file must be given as second argument.')

    config_dict = load_config(configfile)
    nodeid = int(nodeid)
    N = config_dict['N']
    t = config_dict['t']
    k = config_dict['k']
    delta = int(config_dict['delta'])

    network_info = {
        int(peerid): NodeDetails(addrinfo.split(':')[0], int(addrinfo.split(':')[1]))
        for peerid, addrinfo in config_dict['peers'].items()
    }

    inputs = [Field(random.randint(0, Field.modulus-1)) for _ in range(k)]

    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()
    loop.set_debug(True)
    try:
        if not config_dict['skipPreprocessing']:
            if nodeid == 0:
                os.makedirs("sharedata/", exist_ok=True)
                print('Generating random shares of triples in sharedata/')
                generate_test_triples(triplesprefix, 1000, N, t)
                print('Generating random shares of 1/-1 in sharedata/')
                generate_random_shares(oneminusoneprefix, k * int(log(k, 2)), N, t)
                os.mknod(f"{sharedatadir}/READY")
            else:
                loop.run_until_complete(wait_for_preprocessing())

        programRunner = ProcessProgramRunner(network_info, N, t, nodeid)
        loop.run_until_complete(programRunner.start())
        programRunner.add(0, butterflyNetwork, k=k, delta=delta, inputs=inputs)
        loop.run_until_complete(programRunner.join())
        loop.run_until_complete(programRunner.close())
    finally:
        loop.close()
    # runButterlyNetworkInTasks()