import socket

pingIP = "225.0.0.9"
pingPort = 19000
pingData = bytearray.fromhex("c0a8010546510000")

serverSocket = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
print("UDP Server Configured")

serverSocket.sendto(pingData, (pingIP, pingPort))

response = serverSocket.recvfrom(256)
print(response)
