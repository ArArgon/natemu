static void test_ljmp(void *mem)
{
	unsigned char *m = mem;
	volatile int res = 1;

	*(unsigned long**)m = &&jmpf;
	asm volatile ("data16 mov %%cs, %0":"=m"(*(m + sizeof(unsigned long))));
	asm volatile ("rex64 ljmp *%0"::"m"(*m));
	res = 0;
jmpf:
	report(res, "ljmp");
}
