import simpy
import math
import random
import numpy as np
from datetime import timedelta
from enum import IntEnum

"""
=== MOSEL VARIABLES ===
"""

global schedule  # Staff schedule from Mosel model
global D  # Customer demands from Mosel model
global V  # Controls the min number of staff per time block constraint - from Mosel
global complete  # Will be used to signal convergence
global VERBOSE  # Log output to the console?

"""
=== PARAMETERS ===
"""

BUY_TICKET_TRANSACTION_TIME = 11  # Seconds
RELOAD_CARD_TRANSACTION_TIME = 17  # Seconds
SERVICE_STANDARD = 3  # Max average queue length allowed
TIME_BLOCK_LENGTH = 15 * 60  # 15 Minutes * 60 seconds
TIME_BLOCKS = 68  # Between 6am and 11pm
SIMULATION_RUNTIME = TIME_BLOCKS * TIME_BLOCK_LENGTH  # Seconds
TIME_BETWEEN_QUEUE_RECORDINGS = 60  # How many seconds in between each queue length recording?
SIMULATIONS = 1  # Number of simulation runs to perform

"""
=== UTILITY FUNCTIONS/CLASSES ===
"""


class CollectorType(IntEnum):
    """
    For the purposes of the simulation - mobile and static fare collectors are treated
    identically because we are only concerned with the queue lengths.
    """
    WITH_TERMINAL = 0
    WITHOUT_TERMINAL = 1


class TransactionType(IntEnum):
    BUY = 0
    RELOAD = 1


def get_time_block(env):
    """Returns the current time block between [0, TIME_BLOCKS)"""
    return math.floor(env.now / TIME_BLOCK_LENGTH)


def calculate_arrival_rate(time_block_demand):
    """Calculates the customer arrival rate per second for the given time block"""
    return (time_block_demand * 60) / 54000


def parse_staff_schedule(staff_schedule):
    """Translates the Mosel staff schedule into a 2D array in the format [collector_type, time_block]"""
    staff = np.zeros((2, TIME_BLOCKS), dtype=int)
    for collector, time_block in list(staff_schedule.keys()):
        c = (collector - 1) % 2
        staff[c, time_block - 1] += round(staff_schedule[collector, time_block])
    return staff


def parse_customer_demand(demand):
    """Translates the Mosel demands into a 2D array in the format [transaction_type, time_block]"""
    demands = np.zeros((2, TIME_BLOCKS), dtype=int)
    for transaction_type, time_block in list(demand.keys()):
        demands[transaction_type - 1, time_block - 1] = round(demand[transaction_type, time_block])
    return demands


"""
=== SIMULATION ===
"""


class FareCollector(simpy.PriorityResource):
    def __init__(self, env, capacity, idx, collector_type):
        self.env = env
        self.idx = idx
        self.on_shift = False
        self.collector_type = collector_type
        super().__init__(env, capacity)

    def sell_ticket(self, customer):
        yield self.env.timeout(random.expovariate(1 / BUY_TICKET_TRANSACTION_TIME))

    def reload_card(self, customer):
        yield self.env.timeout(random.expovariate(1 / RELOAD_CARD_TRANSACTION_TIME))

    def get_queue_length(self):
        return len(self.queue) + len(self.users)


class Station(object):
    def __init__(self, env, schedule):
        self.env = env
        self.schedule = schedule
        self.booth = []
        idx = 0
        # Add fare-collectors to the station booth
        for i in range(max(self.schedule[CollectorType.WITH_TERMINAL])):
            self.booth.append(
                FareCollector(self.env, capacity=1, idx=idx, collector_type=CollectorType.WITH_TERMINAL)
            )
            idx += 1

        for i in range(max(self.schedule[CollectorType.WITHOUT_TERMINAL])):
            self.booth.append(
                FareCollector(self.env, capacity=1, idx=idx, collector_type=CollectorType.WITHOUT_TERMINAL)
            )
            idx += 1

    def allocate_staff(self, collector_type):
        """Ensures the correct number of staff are on shift at the start of each time block"""
        on_shift = self.count_collectors_on_shift(collector_type)
        required = on_shift - self.schedule[collector_type][get_time_block(self.env)]
        if required > 0:
            # Too many staff are currently working, remove the required amount
            self.env.process(self.remove_fare_collectors(required, collector_type))
        elif required < 0:
            #  Not enough staff working, add the required amount back in
            self.add_fare_collectors(abs(required), collector_type)

    def start_shift(self):
        while True:
            # Checks staff schedule and adds/removes fare-collectors as required
            self.allocate_staff(CollectorType.WITH_TERMINAL)
            self.allocate_staff(CollectorType.WITHOUT_TERMINAL)
            # Wait the length of one time block
            yield self.env.timeout(TIME_BLOCK_LENGTH)

    def remove_fare_collectors(self, num_to_remove, collector_type):
        """
        To mimic the behaviour of removing staff, we request a fare-collector using a higher priority
        than customers (-1). Once we have the fare-collector, we set it's on_shift boolean variable to False
        which prevents other customers from requesting this fare collector until on_shift = True again
        """
        on_shift = list(filter(lambda x: x.collector_type is collector_type and x.on_shift, self.booth))
        for i in range(num_to_remove):
            with on_shift[i].request(priority=-1) as request:
                yield request
                on_shift[i].on_shift = False

    def add_fare_collectors(self, num_to_add, collector_type):
        """When we need more fare-collectors, request one and change their on_shift boolean to True"""
        on_shift = list(filter(lambda x: x.collector_type is collector_type and not x.on_shift, self.booth))
        for x in range(num_to_add):
            on_shift[x].on_shift = True

    def count_collectors_on_shift(self, collector_type):
        """Returns the number of collectors of a given type that are currently on shift"""
        return len([x for x in self.booth if x.collector_type is collector_type and x.on_shift])

    def get_fare_collectors_on_shift(self):
        """Return the fare-collector instances that currently on shift"""
        return list(filter(lambda x: x.on_shift, self.booth))


def select_fare_collector(station, transaction_type):
    """Customers always select the fare-collector with the shortest queue. Return the selected fare-collector"""
    # Get all fare collectors currently on shift
    on_shift = station.get_fare_collectors_on_shift()
    if transaction_type is TransactionType.RELOAD:
        # Customer is reloading their prepaid card, therefore the fare-collector must have a terminal
        # Filter out staff that don't have a terminal
        on_shift = list(filter(lambda x: x.collector_type is CollectorType.WITH_TERMINAL, on_shift))
    queue_lengths = list(map(lambda x: x.get_queue_length(), on_shift))
    return on_shift[queue_lengths.index(min(queue_lengths))]


def go_to_station(env, customer, station, transaction_type):
    """This function imitates a customer arriving at the station, selecting a fare-collector and queueing"""
    fare_collector = select_fare_collector(station, transaction_type)
    with fare_collector.request() as request:
        yield request
        if transaction_type is TransactionType.RELOAD and fare_collector.on_shift:
            yield env.process(fare_collector.reload_card(customer))
        elif transaction_type is TransactionType.BUY and fare_collector.on_shift:
            yield env.process(fare_collector.sell_ticket(customer))


def simulate_customers(env, station, demands, transaction_type):
    """Customers with odd indexes are buying tickets, even are reloading prepaid cards"""
    customer_idx = 1 if transaction_type is TransactionType.BUY else 2
    while True:
        arrivals_per_second = calculate_arrival_rate(demands[get_time_block(env)])
        time_between_customers = round(random.expovariate(arrivals_per_second))
        yield env.timeout(time_between_customers)
        env.process(go_to_station(env, customer_idx, station, transaction_type))
        customer_idx += 2


def record_queue_lengths(env, station, queue_lengths):
    """Every minute, record the length of the queue for each fare_collector on shift"""
    while True:
        queue = []
        on_shift = 0
        for collector in station.booth:
            if collector.on_shift:
                queue.append(collector.get_queue_length())
                on_shift += 1

        avg_queue_len = (sum(queue) / on_shift) / 60
        time_block = get_time_block(env)
        for t in range(time_block, time_block - 4, -1):
            if t < 0:
                break
            queue_lengths[t] += avg_queue_len

        yield env.timeout(TIME_BETWEEN_QUEUE_RECORDINGS)


def start_simulation():
    # Get data from optimization model
    global complete
    global schedule
    global D
    global V
    global VERBOSE

    staff_levels = parse_staff_schedule(schedule)
    demands = parse_customer_demand(D)

    # Holds the running average of the avg queue length in each time block
    avg_queue_lengths = [0] * TIME_BLOCKS

    # Start simulation
    for i in range(SIMULATIONS):
        if VERBOSE:
            print(f"+ Starting simulation...")
            print(f"--- Run {i + 1} of {SIMULATIONS} Completed")

        # Initialise simulation environment
        env = simpy.Environment()
        station = Station(env, staff_levels)
        # Allocate staff
        env.process(station.start_shift())
        # Start Recording queue length
        queue_lengths = [0] * TIME_BLOCKS
        env.process(record_queue_lengths(env, station, queue_lengths))
        # Start simulating customers with prepaid cards
        env.process(simulate_customers(env, station, demands[TransactionType.RELOAD], TransactionType.RELOAD))
        # Start simulating customers buying tickets
        env.process(simulate_customers(env, station, demands[TransactionType.BUY], TransactionType.BUY))

        env.run(until=SIMULATION_RUNTIME)
        for t in range(TIME_BLOCKS):
            avg_queue_lengths[t] += queue_lengths[t] / SIMULATIONS

    complete = 1
    # Check if service standard has been violated in any time blocks
    for t in range(TIME_BLOCKS):
        if avg_queue_lengths[t] >= SERVICE_STANDARD + 1:
            # Service standard violated - add an extra member of staff
            # t+1 because V is from Mosel indexed from 1 instead of 0
            on_shift = int(staff_levels[CollectorType.WITH_TERMINAL][t]) + int(
                staff_levels[CollectorType.WITHOUT_TERMINAL][t])
            V[t + 1] = on_shift + 1
            complete = 0

            if VERBOSE:
                print(f"--- Time Block {t + 1} had an average queue length of: {round(avg_queue_lengths[t])}")
                print(f"--- Increasing minimum staff from {on_shift} to {on_shift + 1}\n")
            break

    if complete == 1:
        # Optimisation-Simulation complete
        # Mosel model will print the schedule
        ...


if __name__ == '__main__':
    start_simulation()
