"""TEMPORARY diagnostic: the OLD effect recorder (each writer opens the shared file with O_APPEND on its own descriptor and writes one line) under
simultaneous writers. Counts lost lines."""
import os, sys, tempfile, threading
n, rounds = 3, int(sys.argv[1])
lost_rounds = lost_lines = 0
for r in range(rounds):
    path = os.path.join(tempfile.mkdtemp(), "effects.txt")
    gate = threading.Barrier(n)
    def w(i):
        gate.wait()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        os.write(fd, (f"b{i}\n").encode()); os.close(fd)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    got = open(path).read().split()
    if len(got) != n:
        lost_rounds += 1; lost_lines += n - len(got)
print(f"OLD RECORDER: {lost_rounds}/{rounds} rounds lost a line ({lost_lines} lines lost) with {n} simultaneous writers on {sys.platform}")
