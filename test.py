maxnum = 0
mountain_num = 0

while True:

    for i in range(8):
        mountain_h = int(input())
        if mountain_h > maxnum:
            maxnum = mountain_h
            mountain_num = i

    # The index of the mountain to fire on.
    print(mountain_num)