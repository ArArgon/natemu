import copy
import unittest
from pathlib import Path

import check_i4v_address_concurrent as oracle


def tables(data=0x300000):
    result = {at: 0 for at in range(0x1000, 0x5000, 4)}
    result[0x1004] = 0x2007
    result[0x2008] = 0x302007
    result[0x2400] = data | 7
    return result


class AddressModelTests(unittest.TestCase):
    def test_subpage_and_final_a20_are_distinct_from_native_offset(self):
        for a20 in (0, 1):
            actor_tables = tables()
            original = dict(actor_tables)
            off = oracle.resolve(actor_tables, [0x10, 0x200123, 0xFFFFFFFF, 0xC093],
                                 0x100040, 4, "write", False, a20)
            self.assertEqual(off["linear"], 0x300163)
            self.assertEqual(off["gpa"], 0x300163 if a20 else 0x200163)
            self.assertEqual(off["reads"], 0)
            self.assertEqual(actor_tables, original)
            paged = oracle.resolve(actor_tables, [0x10, 0x400123, 0xFFFFFFFF, 0xC093],
                                   0x100040, 4, "write", True, a20)
            self.assertEqual(paged["linear"], 0x500163)
            self.assertEqual(paged["raw_gpa"], 0x300163)
            self.assertEqual(paged["gpa"], off["gpa"])
            self.assertEqual((paged["reads"], paged["writes"]), (2, 2))
            self.assertEqual(actor_tables[0x1004], 0x2027)
            self.assertEqual(actor_tables[0x2400], 0x300067)

    def test_fetch_routes_to_masked_physical_source(self):
        route = oracle.resolve(tables(), [8, 0x400123, 0xFFFFFFFF, 0xC09B],
                               0x2000, 2, "fetch", True, 0)
        self.assertEqual((route["offset"], route["linear"], route["raw_gpa"], route["gpa"]),
                         (0x2000, 0x402123, 0x302123, 0x202123))

    def test_linear_wrap_does_not_change_offset(self):
        route = oracle.resolve({}, [0x10, 0xFFF00080, 0xFFFFFFFF, 0xC093],
                               0x100340, 4, "write", False, 1)
        self.assertEqual((route["offset"], route["linear"], route["gpa"]), (0x100340, 0x3C0, 0x3C0))

    def test_address16_truncates_sum_before_segment_addition_and_uses_same_operand(self):
        for cpu in range(4):
            regs = {"EAX": 0x10203000 + cpu, "ECX": 0x50607000 + cpu,
                    "EBX": 0x1234FFF0, "ESI": 0x56780030 + cpu * 256, "EDX": 0x33440000 + cpu}
            offset = oracle.operand_offset(regs, 1, True)
            self.assertEqual(offset, 0x20 + cpu * 256)
            self.assertEqual(oracle.operand_offset(regs, 2, True), offset)
            route = oracle.resolve({}, [0x10, 0x200123, 0xFFFFFFFF, 0xC093], offset, 4, "write", False, 1)
            memory = dict(enumerate([0xA5] * 4, route["gpa"]))
            for generation in (1, 2):
                result, after = dict(regs), dict(memory)
                for step in range(3):
                    result, after = oracle.execute_instruction(result, after, step, generation, route)
                self.assertEqual(result["EAX"], regs["EAX"] if generation == 1 else regs["EDX"])
                self.assertEqual(result["ECX"], regs["EAX"])
                self.assertEqual((result["EBX"], result["ESI"]), (regs["EBX"], regs["ESI"]))

    def test_limit_fault_precedes_paging_and_preserves_control_inputs(self):
        for offset, limit in ((0xFFFFFFFE, 0xFFFFFFFF), (0x100040, 0x100042)):
            data = tables()
            original = dict(data)
            with self.assertRaises(oracle.GuestFault) as raised:
                oracle.resolve(data, [0x10, 0x400123, limit, 0xC093], offset, 4, "write", True, 1)
            self.assertEqual((raised.exception.vector, raised.exception.error, raised.exception.address), (13, 0, 0))
            self.assertEqual(data, original)
        cs = [8, 0x300123, 0x2002, 0x409B]
        self.assertEqual(oracle.segment_linear(cs, 0x2000, 2, "fetch"), 0x302123)
        with self.assertRaises(oracle.GuestFault):
            oracle.segment_linear(cs, 0x2002, 2, "fetch")

    def test_legacy_permission_and_nonpresent_faults_do_not_dirty_leaf(self):
        for leaf, expected in ((0, 2), (0x300005, 3)):
            data = tables()
            data[0x2400] = leaf
            with self.assertRaises(oracle.GuestFault) as raised:
                oracle.legacy_walk(data, 0x1000, 0x500163, "write")
            self.assertEqual((raised.exception.vector, raised.exception.error, raised.exception.address), (14, expected, 0x500163))
            self.assertEqual(data[0x1004], 0x2027)
            self.assertEqual(data[0x2400], leaf)

    def test_only_current_three_instruction_sources_are_selected(self):
        for address16 in (False, True):
            for generation in (1, 2):
                chunks = [oracle.source_instruction(address16, generation, step) for step in range(3)]
                self.assertEqual(chunks[0], bytes.fromhex("89c1"))
                self.assertEqual(chunks[1], bytes.fromhex("678908" if generation == 1 else "678910") if address16 else
                                 bytes.fromhex("890b" if generation == 1 else "8913"))
                self.assertEqual(chunks[2], bytes.fromhex("678b00" if address16 else "8b02"))

    def test_precise_cs_and_ds_fault_prefixes(self):
        regs = {"EAX": 0x10203000, "ECX": 0x50607000, "EBX": 0xFFFFFFFE, "ESI": 0, "EDX": 0x1000C0}
        context = {"cs": [8, 0x300123, 0xFFFFFFFF, 0xC09B],
                   "ds": [0x10, 0x200123, 0xFFFFFFFF, 0xC093],
                   "paged": False, "a20": 1, "address16": False, "cr3": 0x1000}
        memory = dict(enumerate(oracle.SOURCE, 0x302123))
        original_tables = tables()
        first = oracle.architectural_step(context, regs, memory, original_tables, 1, 0)
        self.assertTrue(first["completed"])
        self.assertEqual(first["registers"]["ECX"], regs["EAX"])
        ds_fault = oracle.architectural_step(context, first["registers"], memory, first["tables"], 1, 1)
        self.assertEqual((ds_fault["completed"], ds_fault["rip"], ds_fault["fault"]), (False, 0x2002, [13, 0, 0]))
        self.assertIsNotNone(ds_fault["fetch"])
        self.assertIsNone(ds_fault["operand"])
        self.assertEqual(ds_fault["registers"], first["registers"])
        self.assertEqual(ds_fault["tables"], original_tables)
        context["cs"][2], context["cs"][3] = 0x2002, 0x409B
        cs_fault = oracle.architectural_step(context, first["registers"], memory, first["tables"], 1, 1)
        self.assertEqual(cs_fault["fault"], [13, 0, 0])
        self.assertIsNone(cs_fault["fetch"])
        self.assertIsNone(cs_fault["operand"])
        self.assertEqual(cs_fault["memory"], memory)

    def test_source_selection_rejects_poisoned_unmasked_page(self):
        context = {"cs": [8, 0x400123, 0xFFFFFFFF, 0xC09B],
                   "ds": [0x10, 0x400123, 0xFFFFFFFF, 0xC093],
                   "paged": True, "a20": 0, "address16": False, "cr3": 0x1000}
        regs = {"EAX": 0x10203000, "ECX": 0x50607000}
        memory = dict(enumerate(oracle.SOURCE, 0x202123))
        memory.update(enumerate([0xCC] * len(oracle.SOURCE), 0x302123))
        first = oracle.architectural_step(context, regs, memory, tables(), 1, 0)
        self.assertTrue(first["completed"])
        self.assertEqual(first["fetch"]["gpa"], 0x202123)
        wrong = dict(context, a20=1)
        with self.assertRaisesRegex(ValueError, "source bytes"):
            oracle.architectural_step(wrong, regs, memory, tables(), 1, 0)

    def test_incomplete_report_is_not_accepted(self):
        with self.assertRaisesRegex(ValueError, "missing requested address identity"):
            oracle.check({})

    def test_sixty_contexts_have_correct_distinct_aliases_and_terminal_limits(self):
        for n in range(1, 5):
            for actor in range(n):
                for profile in range(6):
                    table = oracle.initial_tables(profile)
                    for cpu in range(n):
                        c = oracle.configuration(profile, n, actor, cpu)
                        fetch = oracle.resolve(dict(table), c["cs"], oracle.EIP, 2, "fetch", c["paged"], c["a20"])
                        self.assertEqual(fetch["gpa"], 0x202123 if profile == 1 else 0x302123)
                        regs = {"EBX": c["ebx"], "EDX": c["edx"], "ESI": c["esi"]}
                        offset = oracle.operand_offset(regs, 1, c["address16"])
                        data = oracle.resolve(dict(table), c["ds"], offset, 4, "write", c["paged"], c["a20"])
                        expected = ((0 if profile == 2 else 0x200000 if profile in (1, 3) else 0x300000) +
                                    (0xC0 if profile == 2 else 0x143 if profile == 3 else 0x163) + cpu * 256)
                        self.assertEqual(data["gpa"], expected)
                        if cpu == actor and profile >= 4:
                            terminal = oracle.configuration(profile, n, actor, cpu, True)
                            self.assertNotEqual(terminal["identity"], c["identity"])
                            if profile == 5:
                                self.assertEqual(terminal["cs"][2:], [0x2002, 0x409B])
                                with self.assertRaises(oracle.GuestFault):
                                    oracle.packet(17, terminal, 2, 1, 1)

    def test_native_expected_bytes_match_actual_retained_exports(self):
        fixtures = Path(__file__).resolve().parent / "i4v_producer" / "testdata"
        for name, body in oracle.BODIES.items():
            directory = "step-8-low" if name == "copy" else "address-" + name
            self.assertEqual((fixtures / directory / "body.bin").read_bytes(), body)

    def test_native_address16_fault_has_truncated_scratch_without_load_commit(self):
        frame = {"x": list(range(31)), "q": [[0] * 16 for _ in range(32)], "sp": 0x1234,
                 "nzcv": 0xA0000000, "fpcr": 0, "fpsr": 0}
        frame["x"][6], frame["x"][10] = 0x1234FFF0, 0x56780330
        fault = oracle.native_frame(frame, 3, 2, 0x30000, 0xA0080, fault=True)
        self.assertEqual(fault["x"][20], 0x320)
        self.assertEqual(fault["x"][4], frame["x"][4])
        complete = oracle.native_frame(frame, 3, 2, 0x30000, 0xA0080, value=0xFEDCBA98)
        self.assertEqual(complete["x"][4], 0xFEDCBA98)
        self.assertEqual(complete["x"][6], frame["x"][6])
        self.assertEqual(complete["x"][10], frame["x"][10])

    def test_packets_bind_architectural_eip_segment_linear_and_physical_source(self):
        c = oracle.configuration(1, 3, 2, 0)
        p = oracle.packet(17, c, 2, 9, 1)
        self.assertEqual(p[7:9], [0x2002, 32])
        self.assertEqual(p[11:14], [0x402125, 0x202125, 9])
        r = oracle.reply(17, c, 2, 9, 1)
        self.assertEqual(r[9].to_bytes(8, "little"), oracle.BODIES["store_edx"])
        self.assertEqual(r[11:], p[11:])

    def test_source_writer_uses_ds_32bit_offset_and_only_paged_writer_sets_dirty(self):
        for profile in range(6):
            for actor in (0, 1):
                context = oracle.configuration(profile, 2, actor, 0)
                table = oracle.initial_tables(profile)
                route = oracle.source_writer_route(context, table)
                self.assertEqual(route["gpa"], 0x202123 if profile == 1 else 0x302123)
                self.assertEqual(table[0x2008], 0x302007 if actor == 0 else 0x302067)
                if actor == 0:
                    self.assertEqual(route["offset"], 0x4020A3 if profile == 2 else 0x102000)
                else:
                    self.assertEqual(route["offset"], 0x20A3 if profile == 2 else 0x2000)


# Literal produced report retained as a compact test fixture, not an oracle.
# i4v-address-concurrent-dev-20261026-01/address-subpage-n1-actor0
# Original command.stdout SHA256: cda5987d8264bf7553338ce6119ced195dc6f4743beb98d5d0d25846cb9da2df
# The retained run was intentionally oracle_failed while acceptance was closed;
# its native output and seven real compilations were independently audited.
_FOCUS_SUBPAGE = (
    'c-rlq>2ljRu!Y}6zj><&5M1C_ak*5COgXyIHk6#@PUYQicY|6jC$c9qagwhNP8^XC7Z7M1Kn?caqe;8zMmMAO{=V-voBx@tmXpW6'
    '@0Qz9Hd^<q>1-~$^K2wLrn85~zPlfPoU9)=qZ^lvw%ulXvZ0-9SH0!`xEL?xSn}nN`>HC+GJ0PI?_#WCwmaYG=I_zczIlAE!%-%y'
    '#ag!B+lSTsetf@Lc9ws$dh93Nc=EnoKIncs`G@7|ljMHr7IFjWik97{!<QEAY`N{0tz6f5vu*qB@Xc-4FJ{YjKJL5r{)?S#zbSqB'
    'c*|dp-Q#dcr`tw1w|Qo`JK8nxmJi$ar`g}jc}}-%y8h#AGFv{_=DwSE@?g@{_1$FN&KAR`;l|%}(^ao$bVZ%HSY-Cs$)A|{%;!OQ'
    'x6j^A<xJ<f<8!&?<kS5b3Rf1<*QYy&<L&p|x7+soYoV-*@_5(&OCdIe)2DlXbR)N3wiQ{(T%F6cdHH`VGw(u{zYuyH{knH@qfzd#'
    '^qDL5ZmZWF*j2#};(cKUIX%J44|tc|=GiX|?%)mF!QIe=c8!}y!X0o2cgLOYwtYA6Hiu5;7r!?E0000000000000000000000000'
    '00000000000000000000000000000000000000000000000000000000000000000000000N_X9E*ovTelc6N^YMMVZO8Ar>8kHW'
    'H@6fH000000000000000000000000000000000000000000000008jQahHuYtH*xQjX(9XZ8y64d$gKPH{Eu0<MOhqYnP4Yvt`%z'
    'qnpB&!Ml)+`u5Y&r-yaB|Mc~1Hkzzf{rzm&Zdbh?Zr%3V*<`kEm)ngTRL(c)mTf;<jc&?p)Xrz^X1wlK)7f0kp3ClSJNa<3Wik8Q'
    '-Cx);SuNJG<=!^+U3>q<jz66(XPfuBS+9LH`7pYvCBw4&ydBGxe9(K-4eR&qCLOMwY-b<SfB$;y9*3{Kmm{WQbjyeB`*iJH|50vc'
    '`5>Q~5UM(aye{fImZhs)sW-n^&%5n#h`!slQh4bKAG&4Nx7*ojIlA$B(htkkC%OEGZqZrU*hPLC%ZGl}9X=l&a@f*$)2{E9lOa>z'
    'P3G-vF`RQXzaMw=*~9GJP+057ck`h@ycO@flOwF>yK|&1$7k)k$4xhWr)4*sb=$Yg&D<`2{HdL7cN?YN>`EurL+RN|B`%iE)f=T&'
    'b|2-6#_Mi*Z>4?f_w_aHS+2IT>1<+`v^m*tm!(_EDsyE~$;DJf6EatayxDK5LY|e*XLTjpaxc4^s*(TJvS**EiE@Z02K{pMvc)%f'
    '=JR0x)<q@%#^^Ked}!p~G7sN5<25IdtCGV@9{F!0x#fF#R#kF0Rg??<S=;L1f5hEMdFManoId1LxD)%dkS4hm6SSO|xmyk`b!R@L'
    'iO<@()aRWA$PX4kThqQq9$C=GcZ=E9TIg-uZ#`<aM_aq~YNEEpU9teOal6_MXMfjjC+~+gFQzLQ)&7fZIefTmhV%~mkL~=lW$eB+'
    '9!?_WU@~8Ak~upwocE7nA*6Y!t@z(=GBmTO#rJu(cwCI{yY*lc=UqD;zn^Wy;L8Qw7A}-^QD*WtHigSx`}wu~nU?!%qlT%SYR8n0'
    'I+{H8Vl%h;$S3Q^=L&B3^z*L=Yc_l#KdKvFvuls?gY(^KRh0|dY>!I(=>@v|0gpENOB;(z8^fiI<)w|)rH%Eajm@QvKd&IkM?UiX'
    'y7{yxUwslqy)gqQB6j<jdeD0n-E%WDgUuH`EH+I?9^1K$NgC4zMH5Z=>lWz<x07Cb<;vGp?HiGEqe;8oKK9*sJ&_%H9Ze~F>$-5&'
    'ZXj^JvvJ<bH@i)5r=PazZ#PeWH|t@?EXj(G3z4xVLqhJWswm6oy^f<|tYYfOWPH<)2aB(FdaGv;<x@)rmtFdr2q1wk>#T92O$(oe'
    'ScWo_|JoSM?f=U6wZr_8hxD&HWY9zUSQh&u7Kgu?sJj;*vC<<}rQ08{-2V-7K!5E4{Z)5ezT~d`$z7Lu?k?PQ^^&{xSKM`NcYUX-'
    'a{1x#_CFUh(b}=dT%p3b*1?S^WjTUeTFg{O7qYXq@s8|}on=<}EXwuB@W<8i$ee0x*(vJ1)S;B@i7Lo5y%AsLat)acvSe@YD#>M!'
    '?3Kq|gshgQrjkDBNKl7TdQU|y(m!)rN2MjYMhaansE`uK>RPs|)R%oC{JmaJ7NnNQ^*h;D%D$-A-()chQjT)@I&zZ|(jxbAF;1Uk'
    '*2v|1*{LH28N2#I51N%e6T9Gir2`JRX}LGK7}sQFnMoyZKI*tbb`(0Gkjs^EOBUtYrR1GF51-4<pgXh5Wl=6(l&q`0jud61?5r~#'
    'smN1zg^pWF9X-mPn8_262jFxJDG$KaHlmbbk{d5Fc>+$JfYUKeBey44<mHm&8OS4W@(5he%aj}R_V}X=dStuoD|L({_a|2*^;E`G'
    'atF>v#&X|s2VSd&l)e<clfrj8YL*J&wL(apYceT)sfMycc1nftS|Q|EF4J+B6u;O^*&%ml6(W-d5VeeDr_={8^}!cfE}1+5c>qoy'
    'fK&%B)xihdnaLB72jKJpNNw;^8@x6%83W3h<Ow)^0#YB$!013b$`DcZ=yk|msS!F4<>iXB`pZ?yGjQ?@Tw~*C*&|I&nt)uUJOmeX'
    'a40vZwIY*eAdkSwBam@|4kl&0)U!;UfII-F^Aqv_q{EQyQr4N2zZAc7ItZ10I{4Qrk!Mo;IzE-scTyvqR0*$DqL3DzN%1=!$V!cH'
    'QYE}ri7Jx^5OqpHc1n%#Y6rAN&?0G(w8*#9B6odeJKc6YT_z2b21=Jni=;);BEQjP>aUQK)%|bqhWWeQudsGA+Vr$&)tcC>fVLZb'
    'axXn@VGp`6`(jPE9BQXlz}Fnu%D?pX==C6{CwTb*@AOxs+194p{vLf+<x@zTtBWE^GnHmhMYTGIZA}xR%}E@#m3dhNAGH!5wn^h|'
    '%BZcm(fK|5u`Uly`XM$n_M=`Xd>xwHDqpZ!KrJ1qly$B(H<Wpu2m66fznO=l<IBHwUF6mAIjfjgVk6b;?zh!>9m<%i1wCvNtCx$B'
    'E04DYG1a9zIY(XAv1!aQ9k$5@%Lg?whi!T}!K}r8Ta!1Xn22y(c8%9xF0Jt%wgqY2g|)-OHo4$XX%~02tuC7;YFBl%E&969Vm{gy'
    '<+ke5#pC;kz6x^kWQ5WjW>H2TkB=o!S#Dk}^3k@silG+kDwX6fo0XI%p=Mugh1zAcG}_CG0o8gKv@?`D7JC*doz~KBLAw{R8)Blx'
    '7;A-%YT3nTiggq-DE3Z_tyHn9kZ<VkgN+(AdFEoJ#2$&|5i2h(rqVet?VQE#ih-4f+~{PSGzBqkQhZ{tq-jW*Nt2a+MhZ%L59uhR'
    'bj0|JnU^AowR}UWu2eIzq*7m{mWd^lN-EY(Iua=nRST*MQ~_vTul>3U@-?YJVp*kyNwv|wQhP<ME!xv*iD;2%nP|VHwL^OwZ%<WA'
    'Ld#xFy&82j;A*VZFl%|MxmC-my;zZ2waF3{ASyLNV^f5A(g51ss~NXOQ>LC-`(SM)+OKLKs{N(8Ma0zORiz$5yQb9YQpck8(9BVp'
    '`T;fh+TUxvQ@f|VwbnQ7g&UhoRl%a-B>hlt>OzHtY6evYsspsg*S=l*aqY9Uf7Y&8dR;w&N+^{}+D26dspM(wXMD3>+80^3qdknu'
    'J5_Qj+EkvoRHqsfO;*FzpsMUqg_BoFx2Qr<EuxiAYoAs>Uz<h{IUq-{-dsCw?V`0a)^1mQF??Yir*@LsA8Oa9y3-`4s!0`&rPetW'
    'cq-;pxM@$JdQ7F4sVoyzm8m4N)1^)&mpaj_6~Xj{N(ogBst<G;SBv%8mL*@3rX#{p>s4l+G~Z}dR>rDe^kyoRAKE*7t5s>MJ88Ho'
    'IKSDdwAGz7T$MQAY*pIoP8zOC-S0OjZFMKDH%YAP+LE=pCk;6X@9Wx<9^q+gPU7#nwq&jDNo!66g6rCnwYn#**;MJ3ZOLlHlh$kl'
    'l`GqlwYn#*ImzYg+LE=pC#~5AP*=7kYjsarv+4NnH7RR#Pnxlf>7H#^`kkYOtAmJdHY+Wn@AQXf%}Rfy*{qL#RSdytnT(eIH~Bca'
    '@l_LwGL)76zPPp@Evve&EyvV;UzBPnrCs~@H5+y62c#Z7|B`~MrA@`!%=(#xJKpt^B;2k5<?_vW?@yO+r@vt%-H>ibH>4ZV4e5q-'
    'dwbGtYQZ|EeY@z8Z^$>~8}beLhI~W5A>WX1Z(qJW*cz>!qxIjThqXG79sT5a^b_a#ub<Znu26-lsG{hSB99@r#gb1qmX|hGmp0ay'
    'Ha3?w#tR#*uY9u6>58@4T32020~qzwdHb*#-ITg={L5lt66R&rF<BQs_^}j&n?yOd$5dfxg~{b6wYtHj#hJsz%dKm&FJW-xDF<(n'
    'Dme&Q8;^tkO+Gkyxa8E6`YdfJ!MspP^E;}{uca~1i8wgRxWOO84_-LM;Nwva&OKG~9!XvK;F^&S4mmFQd!z=qb8vFD$e}WKoZ9?7'
    'n&jh?4<00La2+ZJ-;^-;BbCYLB^kZsO=7Mz`QSd~2KOAFd_Yn&SAQoxhU}KJ*5;TL2gjFuaCY*8LrpQbP=&!yrz!?*lYD=Y^&5O^'
    'oF6Pwa@k21C^^HFW+I|innInb<h*fk&~k$pkRLq1!r)m{4(=;e@>xnYcW?;G2PZI>yo{3F8=QF5d&z`=7Ot5Qb2idCV+zU*t}(^n'
    '4;2Q7n=<)XnVoO)L4OTiGWp;c<^5oh2bU|Y$7<k8vs+PxL@@uG(tNJ8!)|tc*vsnuV5JAwxR89Ww1lkn)oL4`ucg-My}071uA(%D'
    '8E3}4wB~J2ZKXL)JzqVomwaKB*N#K0im5PjbSllctuoK4@6}5^<4dZ#bt>kM7R&{)G8ebnoTy@Qocnfl)IRM6RorZ$`Q%mR@zt20'
    'UmV=X+~7R;?V4y^@0E31MPzMWiA{2Q%LhNlml<H|xv!|!IxdXKEi)hd{`}xu_A>ix?c<tS{UdF!b}QFb?H_4*RkbWWPgH+2xfJE-'
    'b5l#N+C*JJUzO!<CH<Z48TZ{oT1S|fFKVVHK%FPe|L?^8>(-g;pZTfU0LWZOow-5EWt`^q<}K>Y<I$TRtv6@T<b`&cw=hpe9U;|P'
    '+qB4Xdq`gE#A#lgl&G}c%M;W_mRp1NrU$}VUKI$nwsx(#xiu4SL!qGU|1_^W8)-vY8*;0B-a7kWqowLBuhdMn9JxJ8Z{wt3a~PGj'
    'i{rfZ_JQTLU(Kb6wd~~v>!NI$qL!nEJf=62n8+0o6=GgCY7KH9RC{(0!+RiZZ&j^^Dt9sEt3*^vNr<SbldhG@CmOLh-RskUUXTA~'
    'w%ZE}bhWI#NoFsMht(8&U3{2s{Kll@Z!3%&gfYSxVT>?F7$b}k#t37C@t-G*?QJ$HiMwU{Za%z*ZFp16!d4&UsT8}<+tH1_^5guK'
    'lKb~>WutY!dgxnGk%hkCv0K~zkF(9pUNh$pug0-$>3wvf$QSB#Fr4VV8=s!7`@HTZ>3w!T_`M|h(w=G8_tzi$MX#eddg<ktqZ6pM'
    'y7C0RDWzkGO=Uk&Jad*$?fk=L|1#3_KYOd{@q0GY+w_K~`>9_oAIA3JPj~3sIKP~3+PzD3_)S4}&O3X7sXKoI=tud|YIyTd->-J>'
    'B6^(5u<QO_D&6L2iw!~YleZ#{+sOxOYsG%#Wo@o0p8PdSbXBhMs$A7ox$3KOHCN?|SLIS2a7{LMRkl+r%BTP8)8S>ut-clb)?f3q'
    'SOv){NLE4Oi-j*1zF7ET;fsYY)-UIab@b|Q+~{$m$BiE8hIB)^A>EK}NVm5w-FCl8jL&A_{^={-tkeH)Hgb@I9OSr|V=#01&amz6'
    'S-vx5IqQH}2ZXyL?vA)S;_isMBkqp4JL2w$yCds>SO<hKMi?WE5yl8(gfYSxVT>@|3*${I{~d3(!-Vl_I^A@-LT3=aqxo#vX<+G{'
    'kNT)@KOKE~ShxF6`=iZQ?fv<|R8gNCO#7$9!R*uiVDjnf*TV{;+rLLcQ0gDMU}p$PJ^Htn>g<lcUmlh3mPh5MtEtYHL+xLr?^0au'
    'dADDxa~AAdBZ42t`W_;M?^kclmXE_ZGy-@#e@wd;-D1^$IYeeY{8TyY*~Onc>{$2RboTkbUzc#S=coI$6?KJA8$NCLwBgf+Pa8gM'
    '__X2E_V(ZGuM4knWFrST$nncKvQ4|~<o)p+LUQCfOdzBY_HF`U_%uu)*e5wiJDJ$)Ck{tBn$XCHFXzM8@~>aB*UvKqn+%B3Z|tTS'
    'io;*+Ctf|xU`LKkkkN1{-B?xDRZ*qs#OF@*biTp;qxlBuHLJ3b&e07M5Bu36oqjkOA4~r~Oh%+$s+4O!nw4<pvl7Q&Zt}w%#bMO{'
    'WUeA<oINkkSy*IrW-WG4#WQQctOc_c%vvyO!K?+d7R*}wQnMDwHe?&J4cUflL$)E?kZs7eH!IsNOxuR@Y1`st+O{~Ewk<AB+rG*h'
    '?Vn8Ne&Zd|VR(o1>t=K9z|xR{YFS;1Emd2EUf56PK0VXZ>0JLqrgNnc9Hw*4Kks}xx3S6Fqv_nLlzM&bbgops{dDeOdCf)NGcz)k'
    'yF8bB*!eHd=6>VNYHw*=(0Ja9%ea7X0pkM31&j+A7ceehI`<ct&PBE%+mLO@He?&J4cUfl`$ea7tMlpH@?<)<JekfdpPkNq#d_cu'
    'Pv`opRsol<Sq1E`Uj<yAuL5?@tOE9|0{&HETQp^bmPgB@<<atJd9*xQ9xcy0U{*eogB;}e1J?ojlhwLqp1TX{fUDQ61NIl!0T*?y'
    '-;*w`xUdemI$sCuo>>R%|D|=nb)ygImFs|OTlG84QtYRSF3nZ!7WtmdR%i`T!Ni2cQJFRk6BbNZFk!)j1rrubSTJG1gvFojGlp<O'
    'xFOsSZU{Go8^R6YhHzsM@Y#&(Hx>ccuUQ1_U$Y3fK3@dvo>>I!|D{F1a$WhW7XjN%{Wgn$-_*cB9}5Em1_lfa7#J`xU|_(&fPn!s'
    'x(GLf8^R6YhHyi;A>0sd2)EyR9q{*;D(t3czn^nI#p+q0%K}|QHX<95jmSo1BeD_Mh-?<<l7k%NV1e!%TcG>zP67NrD`roY#j<3U'
    'l^0AQFonPr0#gV~Auxr&6ao%b2seZq!VTeua6`Bu+z@UEH<rx)%d@MjVPz=<N*$$+Qb(zy)KTgvb(A_&tOz%R8^R6YhHyi;A>0sd'
    '2shTSzI2MUU=1s4SP|KXY(zF98<CC3Mr0$hS;I;Wa*%^Htbf89)`B&xEQMex1XBo1Auxr&6arHSOd&9ZfP)pn4dI4xL%1Q_5N-%J'
    'gd4(*HLU++c9k`(EQLU+qtsFAD0P%NN*$$+QfG=4;f8QSxFOsSZU{Go8^R6Y#v0a_PO;|CtYOWcn_|sRrdad0vxb$W5ab{SIo|$K'
    '2o`XWgB;}ewHI)GbLrJvT&soDrPCk>Imm(3B?mdkfz?9lB6Vpd<RAw*kh<g`2RV?sNL{2ZImkf{It`>QImkf{q%KkysY?!WkOQfU'
    ')glKu$br;F>LPW?K@M^tb+KCHAO|^+x=39*4RVl!97tWB6FJC14x}zpmrjEm<RAx9mmK6E2UZKIi`1o=kb@lLK<bi%9OOXi{y3>C'
    'SJ8bGm8stlnYeje454xFyZP*6*U!3*eSEiiT;8|6$lOXk%vX~SL%vV#Y&)L3?<OBKqjN<aReip1?^o$e>wdQCXWK97Ln~jjlRd0e'
    'RUXA7`$p8Vt3#EADpW-kn=%%840-SuH<p(+R+l!`mo_$+HpUAZEy~oBjT*SOpRFx&NF)E9&f5nmy3&Pg^kuQI;9oC`E0p5$SnQmb'
    'Vp?ge9E(m>r#Mp<zoto%Xe?e$Op#|aI*iX>9+xOZ=CSxaO^U>0(Q;ynd!x~DEP_p)B1>7snkL1bv1m^kV@B_#(Nk!&v8acYuW1yc'
    'I>jckAVN(F2V_BmVhaDGA$ly3Pn|;YSP-nnLhz(8IWYyi(bBWfHFb(bqoHavI8tu65-f63qXCgp=%ScHifO1Kdu(+IO=RJNniL4g'
    '!Ue??_$L-a2%kCy<FSA_O$uaX0dk_WaXp6Yma}Snv`PasrKn1EijQPbl$sO|$zm486n#iz6j>ahI>q<0xIs;d0Az83Vv6{qQF$zO'
    'Pi;|nN(-(NwbIC*ppVspJ(WZ3rkJ8I6~z!wsZNoREPhgxA|Y9{qG)l4EIN@z5UNuwAd48(q}V@FISLD*rG;w&cp8l}r*+0+k6GlW'
    'FvM_bQZy!u*%VXsC5@0|ag=I{h?HWGH7S~yMJb9Y-jGHm(t50+fl7<`6Ey^vY}OV~YsJ)1SIsW^)644p5Z@>a0gD=oAf_c`t*_Ro'
    'LeJMy3m5g`il2fkNgWOr>qt*&%{ywVlg;0(o)%x~g;icdE@@S<_+_=44-F{RXn;QrBKDnnsb_piRkv_VrNu{!YM!JftJSDANSQ_!'
    '`)+mAXizVx;${njnW#o1qG=GqsIdhta+!rT`)*CNpjB7aZ4JLBbyuUR)fy))MN(^CYJe@~)fLrRBPwdtwwR)@X}~&*r&hnz{#yIE'
    'rdIz*+pFEmwN?8^T3%IFi_a5PP)#mHIr`kx(yKOdM!AHtEXw^bPWFuZPP<=?xTz0b>zF)ac`{n)XOd1UUttl-N(&vR_SveVn#r@g'
    'S_Z))lvP^ldWq2*q0<I`npb<@(qaqNTHCb9a(hT#>%?hZ4d+>Dy_YAbjV!kY?M)Aav%D%0YHjUWb8~AZ-hv1QZU3iv<=IFZ(%O()'
    '<@46r2a7XUo#mC9sg@(RN9irXV6f<)m9~pBk*kxB{c0{ntYt4ZSQll}6tx^R<k4!C9$$*y#}M<fQEQO<p!&FbSco>Y$D?uHLY2Fi'
    '@>R8$=NUv))hQyJR6fy&#pzz3LSgFh-^_NKFxJ^wo5=ktnti_d)b-<e*G`|z!vX;QJlvgFg|pva004iaQFvUh#VGWv)fS@w0N5J^'
    'lmY<2jDr5Iw^=>*lkPC3U~_>GLim$u)BO6olm2z~3xlU%AcPP?2qA<JLI@#*5JCtcgb+dqA%qY@2qA<JLI@#*5JCtcgb+dqA%qY@'
    '2qA<JLI@#*5JCtcgb+dqA%qY@2qA<JLI@#*5JCvyf5p3Ov{^m&lWyEjw_QKFxusAbgl~bas52Lf;jjFOnWtbNgb+dqA%qY@2qA<J'
    'LI@#*5JCtcgb+dqA%qY@2qA<JLI@#*5JCtcgb+dqA%qY@2qA<JLI@#*5JCtcgb+dqA%qY@2qA<JLI~l1#k*{D-)`ISyKcJbyV1?9'
    'tI7;Q2qA<&kGa6rjr`wH9uPtZA%qY@2qA<JLI@#*5JCtcgb+dqA%qY@2qA<JLI~j>%9xW#BogUOCxv%Da}lFWB9TZW5{X12kw_#G'
    'i9{liNF)-8L?V$$Boc{4B9TZW5{dNZrd=$+cDn6)LIFYu;oD&J8Vs(oe<%*3073{Mgb+dqA%qY@2qA<JLI@#*5JCtcgb+dqA%qY@'
    '2qA=!D1amqi9{j_Ac;gGkw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#G>1{;;+_vxL-Ddo*o38q9baR_rP4leI3ZLZw00000'
    '000000000000000008(cV2%Kxp(FqR0D#}T$It2T0RR910000000000000000000000000000000000000000000000000000000'
    '000000DvEiyKJ;=-_5(txSejhespuY+hwr;C?Nm<00000000000000000000;P-$z0uUn@0002^?R)&34j%vj000000000000000'
    '0000000000000000000000000000000000000000000000002^!MOYS|JDPqFa'
)


def produced_subpage():
    import base64
    import json
    import zlib
    return json.loads(zlib.decompress(base64.b85decode(_FOCUS_SUBPAGE)))


# Actual four-owner addr16 report, same audit-only retained status as focus01.
# i4v-address-concurrent-dev-20261026-03/address-address16-n4-actor2
# Original command.stdout SHA256: 138f3bf380fd46c2e647b641b26b4445bc486f27c8333359bcce4a058f7cdf41
_FOCUS_ADDRESS16 = (
    'c-rm#>vG$=?>!FRi(l)R;rY;8>Fmk4j?(S#cI^Bdr-%P%dhZVwC?DfQPSZ})b`69!rXWh90CEWc#s5ua)BAq%ZZf^TUibI+|G(L4'
    'IeS>I`{icRP43pKoB2X^`)(pTZss2!*8TO>r`g@Z{p4NfCY%0#b5t;$ZC2~aJJ(G<-CixFE;-nyn!Tys+L}`?xh7w_?FlFE{x@0b'
    'p@%2iS2J7P-bv~8<eljztHt%z^=gSaV>h{9J*;Q_)$HHt@*@N+j{9%9`Yhrf`&+q%`zE;TKOY=+JDo2#{c<YTc6Gm*t~bN6n|^&e'
    'UrrZS>wbFuRnNC8YDXWI{J)3(VYsT}vdO#mrc?UoVArBwe%$<fEdH;Y=eXp)Uw@j<=F5*-yzUphJe=!x+3S9`n9gsvKZl$D(BG`q'
    'IH&cwv(<Oj2+QAP_^ax54ARpZIn^0qo6bqg*>83y@HYAy>+#N^KG_$HlgD;F-As=UbEak&<KeF1AZLx$J+syI!F{}U-gdqVoy|tB'
    '(aGO6cFu;*ypWuA@~@T6b~cnQ;b-w3>PNNC;*O>6C?}IUvp!vR!lx>>QY|WT&TC~yC%T~Ge>&bgZ~pJQ`G1B!VB0&rH{EYd@-%q|'
    'yo1-?!6AIuOg}98`~4{So8KFWL?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$'
    'Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNI!!9=_Z?geLG)H7gyKQ&GhO+f3sTmlXvggI3yB@'
    'L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{m(a{8y6+^-(iv;OMydcNr=@BTMg-Q3*wo5{Ok'
    'Yw9M8`Lds`C+~tYE?3)4*3-|EcizU}Z0sf<@20z7-@bK|*=n`Eo-e1H)f#o(P1l?GY<@RgZtkTHIoYgVPS^9*<eeNmUCgKVS9j~x'
    '&3qxJHnMv&o&9%Iay$RhU!N(Nt#0q6<XVf?{q*|l<Q*>PX1<)?|BG_`TC8UO$@SGxjWcpL%l^yeO0MWX+~B_W?%(Nst9&}!%s;h1'
    '|KCIZu&w7`Ie}jPqF;X8{M&A&Uw@K&TYeOg5<<!$nC!EuG1`QKaEXh4v#n#@Z>G|i?TSD4%YHrG%va0FJBQQ$w_JUe+xghv_S#kk'
    '7yJ22eyr#H{`WzhL&>_o>DT>oHVCcz*<w1s)suFU)#Cc9U(7$wKMaj`_wZpcw2#x~Ui4DK-C}!=R&sdOb^mbRUwuGJ4rkr0r_1|='
    'Ui{VP>3p*-6eZd<PxM0rPTsw@qG-NzqH6sosrl-zUtVjoy?33`ZqIVHncvK3TJ`<WeytuQv34$I;Uru-8+~&6uhoCO{%bm$d`X?n'
    '!J4g%lw>X0NlHmXiK*Kfq$Frb+0~PR(|>Ijt=GSd{ME|5{1t^mmy;Y}oK#m*k?L|vaaU=na^g}0PF9_ixMCq4RJy2i*qyM5*+Dv}'
    'w1KpNH0nrVfsoF-TPU@vbXI8(X%Fe7(nh7zt_eylDxFl?L0a0^1UZ8f<xTo;(SNJ{D=lqm4V|+tij0jWX^BHgC|VM<B&v+nl6I6='
    'nW`o6;OV&Stv=c2mp!S|=ZoiYu8Hu(&NVci>AA<HPyB4-9X}Y+0yIe;p4#zqkeyz4`ZLWQlMj$^D&fwkh)8H1Rwd+jk;{>P@R!_('
    '{8Kx*FW1S1XZ%y}Pr^U4ql#9_Uf=fWv0ITk$W>8c5gB%9%?Eq6(kl8cBEt?n@4;TJuZrq#FCz_Cb$CHP=~~WRMh;?6dIftedTh()'
    'B9jwqHmA*dbpMy%SG$|TD=%m6%N*2v+KRZjcJf_Ae`g!N{94XkhBS0Mu)<Tje9Ed$HC)=*9^X<qzV#ofJKF}dPv4wg%h462r`!1F'
    'U4}Go+Uv1<qNj5gb??%~Zg<h?d@Bz3WsX$+`0|IFJ5-y~-eAxjH=@Tczm}hJ8PdkX$@k;S>4@{xXmZ-oOB?$$7o84Uakwx0?0vas'
    't<pcMG2usbdM+5TS^S&y->m<Je+1g!3VPiLdSA};HDReB_yBne9-dDZ0;|&&FPlAH2K3y)zJqs<hYz>&jl$XYwNv`%2sj^<_IIn9'
    ')Fkla3|z(qY*w4?`9DlIvwsJOEQr6oldQisOuYYb7PjLq*Po_~20$(7pZ!r+!&!ju7pwaQ|Bqffy2<r}0BS+8fY$%l&jvJ$>$;tO'
    'ncqI#US0Qh!&}6npWa;kJHM9~rmr}mso7SH`bMys;wb*!{~nZMb&pq#>5aU^NVyl(Ihj4I<t=Cf&79pmJoRk1I%>Y{Ur&Yu%z*~`'
    'Z{4<<gVL}obqA$!SLzRK;M+ei4mrpD=HT%@KHi=?-0cbvuj<@IoxP^Bmz2)cna|Z(&ed7Z)oIPS<E=e+D(lWwYyH`J!`XV{*?QC2'
    'dh^+O%h?+0xwAQQu3mTkd7XV=?(7qD;cN|O@6DyNHRQ8bR?gO2&#Kbd=jQEM1wXHhKg2}D3Rj<({rX-_<9;*UJlwY)6uy#$o^dej'
    't9~&!kT#1ooKqONybSGd*`P#DxS6e?eaYpNU6Hrb$!vPJd06*XcQYsz-fg4o&Dq<ub>N)tEb>|=NA~w?J^jtJ{ogOz|Mz#pj(Hp4'
    'KHXj|S2`rxRI@kLTU+J)CD(+ZYVcF7uZGS5xB4EZkbya;iqYlVaYhFo?^8+9yRKMy<CgzJjRK(Me|Y1hdu*OJhz5+OKfYr3ii!g&'
    'j#1#K@BC169co62JOlfkRcBP~W5SiTnsFOibCN5Tf8512yUSZ6zNj_a<JPdI_$<TK<K{>&YYumLbL7?>{{R&&|J#50)e1IFU6tqH'
    '0S7VG20?pLw>&k0Tyi;Hc4l=A$qv~mDAskrzaDQG@NWPy%1%L|5nd<RqwjqkZV4VEFPImeG1-fEmJDwYJa_W^1-QZsCVTNt2tSlu'
    'ksOf)&~_F|9q=|I&p|FoZc7lV+=>N&#T#aka=D&PU^@I`76@JT0V~Mm)J|T4;0c2RP0%vh-+8#+<l%OBp@OdrI9M9bc~sg(X-m0!'
    'xXwglogjMW@y;qb+*SC>D$um-@Bqafyfktmxpq9*1b-PYGj?|J#*0@mkC$CI*kq?%kGymMf6IG-^+4n5GV9<XlP7>z40!++ug_T>'
    'Ytl@3%w2~2Pxb<-7eFD|E0-kCKpug`Yaw1n1ou0qictm1OF5ivK>l)n@QjFFbkh9R>g%H1x7>kTQ0b)grSV0>q+IsFZ3l&rE~RvM'
    'yN8=l^a0TT2Zey=s>9m^+yHp(6oqiA5S=`Ls_sqMA(t=u;M9-U$rF$VV7)p5qyVoN*xAVwfL{O)K(xV$HaJ+Gyy(D-DVO2(y{yx|'
    '2>wRA{m33%2i|_=<}4J`!UAA2@(irLxv6VT_K4lZF2nR>FW!trD;%_<lV>20z`~as@J=W9Cs!0Yc>?kPtcC9a@7Zz(4rV0n55KL&'
    'tCj474-_h4I%$5qb-}X>mo!|VPzib6Hyz$9@p=Xyh^T~vO31sR@9^>opKgNtRBjF$L5rkC(jwoEMQ+E;>CL8JbI7ED(m*+6(jsY*'
    'w8&?MOeCGmR@Z;P>LUMalWz28V0vg;^d{;k=rrjeytrN%49uY$`iF#{@<hEVfY*Kp-4>p^YAqD=k`q2vF^n6PK{Zg>(TOgo_#Z??'
    '<{S0f{crLGNXUx~X78(*t(Zxw=!N!WrG#25RC!kxP4vN4DCNFPjJ?DP>n`xsBP3OlF*O9yC~sm&UbA0<cR3WJ3K`U^4|^6x%?8>W'
    'qRA#`D9Z#X3kT<re{=RG9iBH;lce87SG+6BCWlxJdewcI^n^w_zIa#`r2CHc=o~rbT8j2o`!cy;`GJmSUxv#G+Jo%MiYZaLjd0k0'
    '#UVzg2E8u}V(?yVb6+MG91@1TgR+c>Cx)+svg)!&Qy-L7xvd;+J-m<VQjn9kj;Z-WeXOn?)+J9`ZXUh#L0L{UWa)85QU0GgIK@n|'
    'Dq?f<q4&YaE1fEIFJOR_JC;5!B>22ARA9)FzC^lhQGF<BMQ<-1sVK5^jiSDYdde8%<p}A4r00`PPM*2+T+&BLZ-k(0h0;sEEzeu}'
    'V(E0{As6`UB7Bh;>=M!rFj9#bXT)zYqR8MOts?`1bp6sKHOmpAzM^K*yNb?=mPzj@iYh&x3@*|lKoGzRKm{28VRAOeVWL6O+lp0*'
    '+F<;|NC|Dh=%=RX+xUgCN>i|68*IFK|Iqg6{?XB+Q%47m&RWyPLhEfYviWAo3RM6~06#)e|DkEP01yCOyBbYwqcz55SP913XxbQx'
    '@e-YkW)MWwfZ-~bHUwd?fgWNI!GHn%(|nGga7+6_jNn;w4gi5rgLYiGVUre3S8xFw0mJ}o02F}n9%DPkag5m*o@FFP4S-dEP_R+-'
    '07awmI*_5q!uX^^5k?)&K+6rt0kYYqQ$>N4YB=x_pb$_`%V2A$1^5KY2knFUxvaQBAcEAOBRmFk4BHr>F%%=^3z=Om>A;CG62l>|'
    'sKQPlOn}J<odf6tzyp}W=mCrdIHt%;K~_@a1dIeUBr4iLm2fC92jB&e1TX_q>(H!ET9zDyJWJ6`07U4`b5^Bo3{-=gNq{8kLVm`o'
    'V0A}^3lRFORl(|x3>UEHS*wE89T_f0uW^Hd)g4)HgI$-|l3Lv(LvCRFGFw6okFB`@!pm$)t?rREx0i&=Y)P%|ku@t)z0j6aBOY0^'
    'zPwy$OKNqGthqtx%WO%l?vXX?OVx$8q*nLHniboRnUq@HBQw_byeAC{#~c_gUM!w9D>Tu_cz9w~2;AJ?9YiQYuxOLX^8cHCLWsBo'
    'A45!tRo-a;H07LC;znbYqK4Ai8Ij;gkK7hysr(!ix5sYhP~5)kSAn=~6Uq%}X7wuooOiz;UONte6RQ!c5vvia5vvia5v#pDtahWT'
    'HQ%hKw>|M1@fz_O@fz_O@fz_O@fz{k8^miLb*|4gz50KXk9SBJ+Xpref|zGxoR?xOIV|~91)F%O#OUO|$A$4+VLDfs&lQ$)h4oCK'
    'j+c)LF=K8$ztfM1IQjEtG5vTyd5140OupXUDvWtq57~McH)OE*AwePzi7_b*me`VQn&l12ELKw`Uf#pDJ|+w)JaNbqNi8)<?Ys_|'
    'H)crfuq~aZja#smp!p|JGmesGv=q&ms6(2J9WpW8kbmQc?2b63^`w?J(uVUPDZ>n@IJRYaG$Y)mHd&qbkTek|Yo<qO**#{+9kD~w'
    'kRP&A!jOp+TeeH<_*x!`CefH7amo&fIj&`YG*d^W6Y7xNa@MRVNp(nhF+-Y@8&YZfkUSNJ44vc$eABXiS`Rp6)mS(5N-bHZ^+GKT'
    'CTcfPp)?-pRC3-rq-NP6|HlnEUt!28ibLW`YS~Jy-yKqc%#i+NTYgdN`-b!#<Si)}K;vpxqG?9Z8AVZcNQ&`8CR7+wZDPx?(!Rc!'
    '0fP<sF=oh#a&G9Ahh!`0F*>=ZeOCpT2%32lHQNe<cG-?&FVp*>ryi2xLd&{B3#s*G^p8(#sSbQER{S<xL`{{k+M!1^Z%t>5nlAOU'
    'dg@5|LY2qh0aZ~XrYTNQ)7p~eOpWO!&iE2l*MUVd(Sjx)CQWY3nm$!q%G~qnh;i)&Dz1H@X5%Hz^(va-SBC^LJERRhuZcSRUZ~pu'
    'l3DW+OG|h&Lx$taI$#~WFHmc|8`hSPX@-nHH)NH)tp8Q}xJ0XeWZ#Qn<x<uDkv%Vv%k7sLFi0`dl#?$d%cxr9jB*Jv`nb#7l08@J'
    '{$rbX*l8AOhb{nVlbZQ=l<4)|YSN!(sOn3QCXZT8fOPgm)@h!o(_BZV8MIE*K%2kiSk`L}Bi@oSv<*#W^dULu#IY<K6A5}RPY_0C'
    ')S#W>fp8)VfPjxVsOB0q6Q{330rr0^E6*ks)dr)==X9_S`o@$_WJNR4a~OS;PT!S+`Y{sh;!sv6r7t(@)dF}=j64Roj^svtEX8N3'
    'I+)tN(xGTqkW{E9mZ0Bt3IL(~$KgW;@81Ig)eviJa!Jso{3*yPH-LL~uWN7isQ=BB>kN2IveoZ~?GW-XnPLl&&-oz#B76)xCq5=V'
    'CO#%UCO#%UCO#%UCO-Z<@v(jj2gtZzPCqP$kBJT6nYq<DNVYX){{^2f-hKe&^kc%ik2`gfyY=ehdV1SFytC==bpNOM{anA9=JsFL'
    '(K38Ir<({mzeVan`}&TY_Ug<j`!9F>tbL*FXML~)Uje%5*XtLY<mDf>Irs$f*Mrk!^TN}(5@qnHC3$_)=g;+dy7&($zqIe>-RsAS'
    'q!?e`#*ehWoc|kI*JAf-`SD71<9G+Y(){(fX#0)nAq*iq7rlPk+S=1kLw}N^R>QZA*6Y>wYex?Yc|*It7R9_jDA9KnbMy_stLf}N'
    'wMVbbEJl}H`(9hXoY3Ka?X8XkWVnpk?*7*<YOIDdRzn`Ep^Vi~$7+y)V5ACbN9%B-b&R&A9c@lK+Maf_LG5UZ+R-MpqiyO&+tiJ='
    'sT*xmH`=Cdv`yV;o4V09b)#+SM%&blwy7U&Q$O0KezZ;fXq)=cHua-z>POqukG82FY191*2ve*T@P6#pITMyMVL20)*?P>@W40c%'
    '^_Z>4Y`xzyTkqg|;hb5)nH9up#A?K9#A?K9#A?K9Zw#w#BT`o<uD92I+fN7mQ87wTf)bQCD=~C)X4|pt`ZKfb$PA~3acUS-Uzz&K'
    ')K{jyGWC_IuS|Vq>MK)UIW>$^!-$WGkBN_okBN_okBN_okBN_;#K-qj`Sa?2GdPe}H#he^CS<v8vY0P>EK<JBS)Z(@pRs&>OMw?C'
    'J>C7<RkT=5uU}KoxBV=#_y3y=OPl|UX<Ea==98a0L2G-K-E=v#oi1mNXV;xhkK3)MVB1RXi+(pb>ty|SEHZz%eEhKZ{4mLGxtT8?'
    'hI3$1`RU@J?Yix6SL?6)MZ5RG%VE!UY0~|UyLEpv|MF|5Cme({etBY>&&hCR0yEv1>BdYqX1X!cjhSxDbbIq_K47*z)7vOP2}=An'
    '>1{XtX7=x)JE5g^?Hmfj+jes(Y~Q(?Lt*$Ol}u+d{pQHN7PGIy3<sOxF!SwO_gcS$exD%5DzC2FkI=|~+b#$t7zbYId1$Ao^7<I6'
    'K%p#0m>2~D)Dl8T@<J~ATrDx0XRhF}SHix^D<R`*ibaM`JGdv{{eget@a4J4Fh6)J+7K3_3kPlsd+N40KDg|h7<-q%BWFefKs!F~'
    ')Bs4!bZZ>AHLBx$J$f<d#h@31UJQCM=*6HH<1KqJh}4MGh}4MGh}4MGh}4MGh}7N^Qaj_h4X2*ldgQtFN1j`M)^q#vbh6hvbD#a7'
    'c-VeW{58JZ{FE<O%q#%hVg|P;zAr&E_Q^|l?96pP#F;A=@{BVVh2QPW6;rvunS1D~r5ktr<)FC0mjk{W@a2Fn2Yfl;%K={w_;Nru'
    'E|D6M8j%{28j%{28j%{28j;#}yK(c08&^NJxOd~GBR6jPWo}$|`CQ=mU2}om3+DpI)49O*iMhazbAf*w44+F(#PG!M#PG!M#PG!M'
    '#PG!Mocc=%N>G9lf5FsW_jq2eAPO&USZC(`rth2k>s~nbSH=n{GP3vSD|qaMu&?q$Tr&4JS7W|AT}%!RJEz6oE%3;B0a!U~=N%Xc'
    'ua<OS9Jnw_p$mg947xDr!k`O-E)2Rb=)z#a7?B#08j%{28j%{28j%{28j;!?o%bu&8uQlmJN)_qp4?~W{pRnR_v?P(yx;t2-miUP'
    '-mm-d^M2(*$}1eXsBzqpd+4kEWp3Q(*9Ux4F9+2bz8vu7fG-DpIpE6yUk><kz?TEMaf#H3)QHrG)QHrG)QHrG)QHsH=)B(x+_>Yj'
    '6SnTyamk(6)NvB-cey_IWt@adKuthRKuthRKuthRKuti+Nw}1t1SL2L_tledf9^Z~A2Q|jH^`KFTkZl*qoBKh?gF|C=q{kUfbIgi'
    '3;wQ5DIzr@H6k@4H6k@4H6k@4H6pb?ZNlr%^_p^i>0gP(IctGfoLHP#oLHP#oLHP#oL*BRH6k@4H6k@4H6k@4H6k@4HO?<R^_qIl'
    'FXjAF0%`(k0%`(k0%`(k0%`(k&M&0|B`Cr9rGMo7QqTFNoV7rA0o?_37tmcmcLCi6bQjQFz)UG3H6k@4H6k@4H6k@4H6k@4HO?<R'
    '=QZX0QqEc+7AF=b7AF=b7AF=b7AF>`*OW+&NR3F1NR3F1NR3F1NR3F1^Gk2r1kiKyOU+YWQ*-1sHE(BrDQ7KEf)bQ?`?D4}*NPI9'
    'pv3Pz*Xr3>Qg3lu6`?K%4N6dg5`?;xpadn@YZ2-a>e5UoK?zC_>QaIdlpxe4)FsrV1SKfJL4#125|p3>p)R2=p)MsTK?y=#_F9yn'
    '1SJS{33Ul|DM1NJ5bCnmq68%<L8wcp%Rz$@l%NEmE}s)6C_xEAT|!+B8kC>}B?xsXK?zE**CNy<)TNnFf)bP<)TIO^C_$+E<Djlw'
    'MgK`qCXNu8c>i!ataf|dFXo^6^}N5=pC48a%j@Y{U~ZBhi`DGELGJT(zPXzH+t2=kV0JZSEOq<u^m^6Kbhn<b*7MER_G2ms>d8Ld'
    '0afl79&-ijj+L6uE>L#yvSe~t@~M_sy;Nd?J6jmf6{d5A`CMT+S6I&!>Jn3r3b8KldM?ktU<H`T`erfxD2*4bG~CzQTV46<Ws3^6'
    'MR|1LoZ1$&!a_N^G*xbkOzGk^r7c6Fi`CS&><pHMapvWV618P{bn%|jmf_K*a%x-j2211U@-?|FE2YcTl(z7UF7<>3W^gYBr@&HU'
    '71hdNSi&f`g^6?pq0-g@(iMYhTl)v=^XMu*xvj^eD_|8}f2XaHQ`>4cXgyuGCbxxXuuctDMlyOUL6=P`SPiMITU6V6Vpyk0A6stg'
    '66xAOrL6*_YX#M|>QD8<`aQX=#G|X_l(veMu8vd1%29{xma}4Uw1m}}+LB7SEsms1D3!Jtk}g<O+tNc=phy=H%58BkT{Nh)<$rXM'
    'pxT!E!IC_>a8K4Hc%rVXQ=v4nC*ZN_N>6cExT&@!n7khrQ_5{wBwakIv}KTVsiLZj4(ZZFx_nS>3jyhJL8UGHBg)}*{VX)DuD(;S'
    '$Q*P=7ar4PpTe+!Q)x>v>4HtQExm;0k#rHItji&_1;|QUN=%n1s%^0$EJ*}C#=1aJm;0$$e@lvC0U3&kb*{>G>8F?J{jj)E7*;DP'
    'x_lT~NUbkpNuj5;RM(1nvEsKCmPChxE_8%bs(DvfofJQ-p1Qcy3soNLEkRXu@v@BW1FMM@tnP;u#76WIXMBmO>sm}v7e}k;o<x(;'
    'Yh<iYhGoS@)e%cUy+FmaFAUm66)cB_6$mR97Sv_SbX~JiP1F^uF4S$Ty(YSgCDk$(No&ifm6vwFy5Q9XYK`R-v1D6qOR!<JI$caH'
    'zqJ2V`?y4_e`Mc_VdYZQ{*gT|u+{CC88B2a(v*`gB?D~88RZgU^l|qhCws2eJ%(Q_w}}T29g}A)PX>*C0_vcAUY93}x^5u)XH`dZ'
    'lP9uhgP_ZkCA2y&v8oa35~ato7=5EIESRBfXfmS@$w4QMWwDlLg5Jv$gpnCFXs37}oX7$oWLP^i*Ql8|T|qFw{*Ptl*@z868;mNS'
    ')4@LIB7^BfRx}emhtWsrba}y`OMfQV#R<%X`*FQmNE6HR$}wQIRk=|gOYvE%4yLMV;Uv=ju7+x2L6=}$08H*b-U>w3X9NgTL#(mM'
    'B>|lBry#4`)`UB|*R{nkQU9AMSHCfwwCd0OCeVGc`rNOt7X9?*kvo<|BK>{z&rz>%5{e;_NPlIg@Njo0ox*yx+OSg~kx08vfk=Tw'
    'B59|9Ot$;Ybn|dO#Pl}Pc}pytJ*?OLa&y(oPsHnHn+3dOw$u=OsR$%$iR&gC1SPFaE~o5DD01-hZu7A2uaI+QgUP-`*_*S+GWSE`'
    '*~!jg;iG$$wbn1z$7yTx7FGOod$n9Gk-lbA&E8aRZI$zvT$3*?7jC`)25NbF&3`ASuqkDDLA$fZStajNNzuEmASB@7s*dpccEK6B'
    '(%g9)16=pE3xn712UqM~QE@=UG1id0ddpCA9co4i+>+l}b#8&igez?|<2JPBgm^{L0NQ0WyUSZ6zNj_a<JPdI_#Ew-=14DV4tIHT'
    '<klQA?tYjq|J!@)Y=OrWp(X?zQffiFpuyZ&)WMD`&bGy&&>#UGSwu$Q`OdCIje-V#5jj901J{>x3;y9o4g9p~sYje4mlmXp8YV{I'
    '5I%MI(2LjjlZ9&;;XOn(5M(T&`RL(eM!*2>Y{Vl<)lgrC;~Bnvv>t*B)o4hr!0n6>Ap%4QRKoACK6^N$5z9je1o1?~4>hU@w=^OI'
    '2w5c80)2>&R5+^<cS4*g<rY3b6wbg`jmQe(La8)AKm7U;9_%_qp0jW9LWG49eAybmLYN|l7TuEzk|%%&4+2i`Cg<7$269F62oR7#'
    'Oa<|vVp_l{Y7iX323S7=9wlm=(8DW_XsmTDFj0~Q^#a`U2<spcfT&Wn8n&v4vmpwD#zzRKI*p%7`%Cj9q>E?=q8kX&Kp`R=`G~wC'
    'R)zR4;=NwAOdbG&IfyJFQ37FEgsLp!YY3v?0i@a@J&2b&`1#>d#{<ZwMP(2U%$h2Ih!vhdjxD?^7bMRB5gkNh5K_vi1!Coj<RKt5'
    'jKClQ)Wx>=j$D#F1q5*s`$FWdIE};@#4ZpS(lANU051Z%5djTEa5Q$}HEM+5YeHlVfnY=f5sXH32P$C^<Up_t@h-&b5G966ID|V8'
    '2SRKLu_%OeRV5GvL8Kf(#S~kR2pU0)q(#yq-;PCY$IR_pDh-qdN(1GPNsFXK(juQ4GFOZ1tJ&)M56B9Un_2dsw>JZg(rieWJYKM&'
    '4p8xz){B0{Xx?7Sic9z@>f7?Iyc!hj5eA|ULoN7%6FyZj`ao3XG;=K-pXh>$r5)zW`DTu}o^4u^bhpxtcwa>u<>{vi)!3Jn5^AmJ'
    'G<RjuL?2v*_3q0=hfAzb+99bv?~>}{VN!kYE{9^EkWc2<N2iIQ+x+_NIpp7*y-A1XP1Ph(X7n(-vTSmQ)j-wvWn$n)>^UBm1u?2<'
    'kIs=}uBE6E?#twY<%e^JWw@Ns5<ItM#gr)HQ#fqDBAsS3I&|#Ig7l1DJF$J4TyRL5<G3%&v6QOGmHV>lvUi%%xht!3TRGZ#cpufJ'
    'ASXAP=i2<CK2}!`>yjreH;>Nepe&~vvgT2?31{#}W>v)I=Bt`H8a3zEcE=g~@Of$f>a@@F+6zKSt0omj&HuC*zmRS!Low1|pPytP'
    '%7Y|Zt4V>PQkr1+>?8rC21d>Gv=|LFXBr8HPLm3YW)B9<q%{~urA0K;)wSVH(`qq(BPke}g;jH5W6L784LWGbvT0p4+C;O8UF%pi'
    'g;~9SXnTy4nrk{tm(cuH+r}zQs5Nc4Mz^5(uC9%%L6gfh?bf$`S#x|{8=5qw*fyY|8O){)4FDFJVhk*yY1Eq6Y8u$VsI1BUu0a#c'
    'bG0o~SaWMlOOc1ZY2K?kUbW-OLk6yI>C2k(>ss=@<`>&GWk3__jSfY?F}9^5Y8G-BCKi~MC=A=wlz*pCs7*AmZ6wwlThp*eOF!3a'
    'U#lZoOD{HUFb59Q>|fW0UCny8ZQ_6?*<&2jRBJ#<P4%}8$>{U~)1VwuWi|ERwR~Pp$+m6a*9>q?tknquamf0$ErDNC-c7>}fSmvY'
    '(di(Mfg7r%Ik|O65{^T@zis2ela{43x!_2ynt2|(XRS)x_(fB9UOx2(R(E8$z=zLT6|C;aa5Z6f)SzH>N7g$`61d2g)ao7?@{o>y'
    'kuB*ol1J9ukn?4>w4D@kY|TSv`9-#*R`<x7hv_mG*^+9+BWrH+04}p7wYo>vJmlM7WJ_vwkF0r^V{wr!sntEQ<~BuV#H7^f9+|Pe'
    'z&vSKIz3`<xSDYPoLRLj>u1Nq6SLY*zub+;Ay~A@Fd5UO5_}9X?Pg_4x16&|w8@#GhSJ*E)o%x1>G(M)Zjas0p}2k7uL5z~CX^ef'
    '&b$i1xl(>Vz;+w~Cs-p`BUmF?BUmF?BUpQTVC@DUSG&4dPj7nyHUc&RHUc&RHUc&RHUc&Rwzmk_J|e+?mtej9a@an!c@V}t8|Azd'
    'Wc^;B4ucW!(_#E{7(X4xPlxf-Vf=I$KOM$Thw;;4f8(dawg`EcPI3Ba-8kZo{6q0EKdnoAOngjyOngjyOngjyOnm(J;^WolWkbgO'
    'a{6I0EL<^sY35dEA=%cH{g=(;-R=`0M=P^A{j!bD)yW47xw-8^Zt};c`TZPCS@)mU(IPRGlV#m7_37%&3HvX1{Y+PQ`)MC6!B>E8'
    '`t|w+CwcjYZSc{dMSrzj7;k$T;I0>)#+4{TM=i<gb3T8r&(j6g9*OXW_tR^9>PY(8$>;Taql=g1{pI}M(7IT+WW8E`yi(0L-XV`|'
    '{q?wLjqfHc`|BYHAv+iS^uE`JzV2s>>3q9t$|pH!HGJA=y<TlUcJ#22*R$*EwJy!FE72DftidOhZ$vehv;JzT>!`ec?=`amCRZ^1'
    ')l{S^?Ecrj>R3WX%i{9xf9;`0>qw(@<k33HXdQL54nX0NN~|5P#f{f8-mZ4MVeNR!+VQ5f<85ol8`qAvt{ZP%H{QB#ymj4p>$>sQ'
    'b>pq;##`5ox2_v+T{qske!O-4c<cJ{*7f79>&IKykGHNLZ(Tp$x_+#6F-)u;)~(Y6f*Ct2XUlT7EN9CypO5)`%;#f1AM^Q`&-YvA'
    '^9{ZcPQKve3r@ZuSR+^?SR+^?SR+__i(qXVwz@j;y}kAWfI8@picx|Rl%T{}iJ_Y_+m3D5pP6k(W;lC{v&Wbn%j{TY$1*#X*|E%y'
    'Wp*sHW0@Vx*<+kNMtn?sOngjyOngjyOngjyOnm$#KE9vIpI7&r!GXNGxw-E*lXpnInJnhZ9!r^Tv(_i;>1WwBq`?c8p6-6_YFezO'
    '*RQJR+kUFq`~OXb70`diY_4Jb^U2Sh$F)7nZcd%q&Z#rUQ}FD`EV|vY3$`r?zvy>!xK5Ut$6EA<tIQ8;)DLs<mYezVVK|2re3~vE'
    '+OFIFcD4SxU)y^hz8v;!S1{f0xLfx(^Dn<<hQdKm<Co{Q`J4<VCotEIxo*sLW3C%>-I(jfT(>vBG6ZJjGr^4#l%T|Kli+sKZ)X1<'
    '`V(4m*Uq6Zylpp!!uFlJITVIpQpt2S({GdPYccyO%y6(74m01rb+7e11a$o|R(W;Zeyc_X+;%}I!8q_r&qF&!mDk5m1qx+3!o-N>'
    '=1T}A$qTvcbG5{1p1Fd@UJ3guuY`=NDHa(%?cknJ_YWKthi}gXU$ANJiZ+JD=)!^D!k+pqjt?$7FUH;{@W`9d5YUbvdo|!U)ivqq'
    '*EsNNRL41c^kdMEK|col81!S%k3m1ioAzT6su8LYsu8LYsu8LYsu8LYs=X<wcE))dPMx>)$a(9JoVWh0^Y%MZ%3kZxefHbqVf*dz'
    '*Z6buQ~q4BvjBLDdEBD;z64R(Cokc#L)ZNfhpw2&a}Hg6-u=5Bx?(JsICKyEwRGi<zaJDA_<q3m1HK>d{ebTWd_Um(0pAbk$|Y1I'
    'R3lU)R3lU)R3lU)R3lV-g)29oxN`NYi+fjYI&$TvU+2nom(K}~-!&)Ly>L!&Je?D4pO_QuI4AhGLGii7L=;aHPZUoSPZUoSPZUoS'
    '&pE)H^+*XyP~vZx1MD78&=o}C<qhl1B;fRYlYrd|CjrY?Aw@>^K79p`-4OOwZitH}0q1JWcjt`B!C~jN*gFOuxi5e#hwXgUSi-v{'
    'ofrpBj3Xxmofvdt(1}4O2AvpmV$g{}CkC^|2-OJH2-OJH2-OJH2-OJH2-V)$1Yj}OA`fE78f(9PfH(Kq3BdXLCIGu1H~~06ngDE{'
    'm;mg4!USNslJW{?E@lspJ97{HwZF`j`}}%=Z|eP^I>Yw^z8~=YfbR!<Kj8ZT-w*hHKvyoI8lf7Y8lf7Y8lf7Y8lf7Y+S{D~e1R)>'
    'e3ru2B|9#;^PD=)#{Dku=e~@yaS5pjsR^kGsR^kGsR^kGsW}^$5|p3>XXCzpHtx@T>HkyazWxU3Qg6#)!08lp7|>xrhXEZ1bQsWK'
    'K!?F!mM%r8MyN)pMyN)pMyN)pMyN)p_UFxf{kfh~PBi^H;W%e65RMa$6OI#(6OI#(6OPk!N~lJtMyN)pMyN)pMyN)pMySS#re{2-'
    '=81`><|)sqIr5yEw=>a{vll2q2}->E*$bR=MF~n!;`g3&_3Uh^w>Z6uP?v)SB`84&LS0Hwf)ebt2z3c{X(p7Q1SJS{DM1NJ5b6@@'
    '66#Wd5|rSeL8wa!N>GAOmr$2bmlBkq1fec_ElN;=5`?;hx`euvpadldb=hlCf)bP<)FssApg{>rP=ZjG&xsP0pah{Vp)LmvN>G9l'
    'gu0ZV1SQyO5$Y1^(o85p2}%&^Qi2kcAk-z)CDf$^B`Co`gHV?el%NEmE}<@=E+r^I2|``=T9lv!B?xs1bqRGTK?zC_>ay3O1SKf('
    '2SHuAivE+JOdKIF@&4f!_|kTh>wYo+)UW6Lz5e{LdRSgh*8+2s{8+4J|3R*Hlh4!n=4$qDKl=}Y*}EEx+}Xd=>s33`-Fm)S&o^J&'
    'kEtA}C;NB@R9TWSHG~v9S8@oZ)ZFdMyon)sS4?Mp!0zli8*MDv6x-PlO*YXP*=>T91&Ad%BGnSBmr6`<XA9%G!gQ`MpDQfq3hS9d'
    'Z_X83yfe(@*%w^<WPP)kew4<GHguD(x3?HtZ0?-tobTkSq-O6rFU?W9V7m~ykh^HQ=)0J@Si2;xV&#^c{I8cGcM`m`l$<@v4J2uU'
    'EbW!$x{JtFk*ifsAaz?gg@d~K(D~d2)5(z`b)k0Abuo7GG?VS5ij=!-y6n50x~w-VXTejDT1C#{Dw34TZd>H+RnA^<8$r%ug^Qd?'
    'p0nJG^Ht7>dnq`D94QCNadMa(B?nnNOF78OK~|rZRPLp6xiTyFZIe_k7i6=@K^2O~RjJ%a<wkDJ%B`XGJQ`6hLK@9Qxd=I}9Ew{B'
    'og62J$x&90vT~G_qpTcd^|49qa+Ii>u+%Q<XrX?%^&)arYL}<yjMOd<&N-=F6vN4LazU;_8c<rR!!CK=@8vK#N)EEBuChb+SUJe5'
    'Hc91jkd=e1JX4!QP8!>0kt-rsMXpNaMk=>bxs_XlW_Y;>d1Ou$!o|=*2RiIao$LvCta6l<qbzpF9;;d|waZafj<Rx;mFH`d$Yqf$'
    'B3DGNO6^8!w^BQlQS8Whxkb@I7vw5LIdCr>_KD_LRnDk7gJ)}{+^X$K-Lgw;Rn7(LmAaE$gXockT^Et7Qn!)1jnplA>_m^9=&=($'
    'cA||AdfY+FI_wc^NKjH(K!#$9YUhG;zFhBx<@!aFohYLdZFEq_4m)LE?qtu?T8dM?T=C^Ja(dBWXn2QxQO&!;>ZJHt^~5P(s`4FL'
    '99@a*fQ5^iShecdi9V0%CC>N~RhNDoU6mB02T7f1GI|Z^)k7y*Jg$y%!k4JH_Ju*asM3i7i~frGiuPL3UJC^s*F>E1Qr+(45Yb&x'
    'UC~@oTq}xeMR8x)0ppaHsC6geqPC*7R<zcNVp`Em+lHl=^uKB!muU5m?0Ye+T&mhXvgZZ1y8SW(hAKvya`L5QfDJjLTtbXK?q1|%'
    '&(*re@GFCEhX)TGlV>bX2916K>Y#j3S3EUbGx}#$M|6`XvS<U?ppM}ghJlA{RH597EJoi54Un4`Z9|h8eMk;EaV#sUhygm|3Bt&X'
    '8njb95Kd$P5HhSCnrqZd9BdNfc|4JoXCpQQZ7`~QP6zt{s}lV<mKDuJ&tddYIw+6Wx9F-oAF+!Qm<#vgdbN-ymgkjYs4xPbeJsUi'
    'sXCads)dtC`@0&di3MGPaRD&7|9C49Ri6<cPz|xhCYQ3qpMtEkJ5J~Kx?0;_^LEPBZwx1``g6YtbYHAK_v@=gKfQV6jwO*ue;@sG'
    ')GM5XVn`&?U)d=<+}%m1uwJb;>=Z~O(ymh=QXr8?+9@ED?S3=eJlqd4z0Gvq63b=}>vg}}T=nuZbd&qpW&v-REj0vRDgudG;=0KO'
    'K}l<q%PG4OiX1$>+dQoME99KnV6rb!_U7zu+9jTy>?{^Ox<^@S{bGHbwl=@kbAGzLTCSEzU$d!ZZ>qPp%K1yK$(NQ3H(vk)wLHD%'
    'zmrqglrp@a-Pz+DcEF<vZ+AgJz{61;;q~pjjR6k(?f<q5gU|1WR_ngI+JI_ftReZ%5A_}VuMWxYsyMd*V=8%9Z`%DIG<<Mzz3$R>'
    'h%agfciIlt6rZC#(-7%p4dE_rh};@NM%oY4<$rr0omB@byxFdKt2MwB)Y~4jhCC48OGErFs5dw@U%PK%Hw}FS1iaxP*H8?+;CTp$'
    '1$7<gAwUyaU{K>qQG-XgK|4fH$`G)F4>=Fs_1FS!2zaSW8J=`Igd1xMf@w&g3{jcbf`AAmSq(VD&F_YoNo~<Nt8t_<1dGxTTGLno'
    '+|Y<vx)xj1z<}2%RP-7%K`15-K|grxHIR)Mly89&jaeWfr}3jY#E9|`Pt(8_;xHQG@hzCBk$|Y-C&UZP5P``<s4uF2Jq^*P&_W~{'
    'sgO(Z8adJ^4uV6a#V<5kmWRkrXptL4q%=s5z^oktFQvuztj40s5OIn_P{bjO5H(t)ksE}AY716ajgi$MoRfxdjn{ZW)G(HYLsD+x'
    '4#e~{0#%17QyyX<L1Pc8MTvZi0%_bfX#5J%T{DDm@(`kl8ihzh_$svEiiX|fioC|2Gz^6JQW=7Ah>B@EK!Zb}1%D8$^@v*_o@<Ao'
    'O=&?H4QJ&cY86`)8*yX}Um!wdhk#OTv1JXil_3xmhd__lP)pE2m_`c`8m}!bXf=XWhA>qcBG#%9v`AVcE%MD+<aW&5zL(NKX`nPv'
    '4w<w_S|ly<sUdT<xW1aLuK$2M5V@IU|9N{eXeZ5oL}vge0s0;2{!8n{!#0|!*Ye;JeilDuSb4P?SRE`!%^r4k$qApT7@CR7oNGpV'
    'bfOC?mUfsg=bJfZceaU0(v665cweR8lWsRvn9#nglu&C$*SIT-Ci>tijCx-ts#;=2_c)}{mt7itI!vPv-sMn?56@-P2k7lCqkel1'
    '`8Q{8(&2eiHA##f9m%dNn;c>_uz`J<j3h>kHXfD*v9)NA&XHrTrKqj#%jAOP2YT*(87?QZl+JBgF(pdB8xGsANY9vz4iWpZAVY}P'
    'zHMJ77aWpiHSWuDETw8X<-V-C?49Ot?#im%R*tqF-bZyQ$jOanwl)u_kJZ(~y5vdA&7+SwD9fpateKN-iWz*6SrxIl_p0WKM$NLd'
    '-Eq!7JYGdpaD!%f8gz9~(yD2M+5sU`Fld6Q4AMw^eSTU&(%}ZlfI)M3HKp*`X#q$E)b0$)zM*AFCmqC*Mp!goFlg?q!C)yZqPeZE'
    'jcJ-li_}x30V9`C2MSRe%^M#2b4^k<tw%<iXdba^{ih}^tM3nOj{(#SFaZOgX0#4tx~9;YHYlT8&<t1C2G5}B<eF&fTZiq2Q3)fS'
    'CJ_&#hvqDsHUhwEG^rS)mnKg80q9^5$8a46Sb_mpGg}*kXxX%;CB;MEG}BdGuG(=811km%0ESeDeAqZ-6t~f;rP3Q6aWJ^pmVBsr'
    '$6=UCU|MQ0Y_rrMAwCXM3kJlZS+%ACj+St)`Mp+0xRy|C+870ZXoe9N08jJW+vwQR>M`DHa<v)2p=QH}VM2jvS;9z;b#=&&k3$mn'
    '0FX52TT^LuN<bX)d~HkF_XBJINCh4ZZF~ou6ct5gwbd-#I-~^;7!>#l&HAKe>D(>2k*nsK$L?9H(l&n4WSy5!wt>|h87_e1vsMMG'
    'J2G5N(H%7?SlyBJ4$}fIvL&^;M}|Bk;9q1*I&tKYH8(_knJsOnfgD@&kV}4%EveN#vgToe%tf}O8u7@QTju{|wxm|~$eM=?`-^Nz'
    't?rRE53?#RvL&^;N7mdX<&2n=THPZv*0+==4NE6P><w2_?4L8MmRJ4kcz9w~+X<Ju5jg~lHW{X0x>SM(^x4hBlx{g^m1xs3MGd93'
    'v#Z|@zRU4*P~0B7okMZ^vR?(_woNEEP@Q=dfQupget_*b=1s6hutu;(utu;(utu=<_Q2W=KBsndv!34e1Z)Is1Z)Is1Z)Is1Z)Is'
    '1Z-~+uzf^I|1QCL`>n8jX!9V9c{a*<DaiW0J{ATe;K#!Fu`qrtj2{c*$HMrrFn%nI9}D}FKNg0z&)@%@FfTf2Y<=Q8VWND35&duX'
    'ov<x99%fRUen&Trcq0E$Xw2{E5*ia46B-j56B-j56B-j5|CP{q^?BJqalf2?SPTnU3}2MF)wxH*Vrc!B4Lr{J*^i_3*PMR8M(5|`'
    'gXP-XcDXiunr41KM?=>AM|8AEY~*C|HcWTAdUU$}%UwUyRos5k2TJgrpPPQYe!(eT`tcfkRA|v(Z5O`Vo&-?q1t)PO%2-iL(#XXV'
    'XZk!{U}X{D`+j=e{`$P0Z~FUT6}@&L|2MR$#opEO<CW^d@eX-Z>#xT}YkU!D*<TOA2HCmjr}y~mP{hvJVmjY0i1JB}S`A+<TCZ2z'
    '?-)HS<YnyodabLj>`L^t1k3A5?Hkd?<*dJ&>JlmM-+N7=fR*Wlt&{(?XBw4=qY`OUB9BUxQHeS#AzlBdh#ePk<09kjYR4Paj<>8G'
    'Z(2LvwsyR6?Re|D@z!<Yt?R~H*NwNX8*g1V-nwqQb=`RDy7AU^<E`t*Ti1`bt{-n*Ki;~2ymkF}>-zE5_2aGU$66O7#Oh()Iz5DL'
    '$ExJCSWb)Ov{)wYF>#NHdraJ8;vN(Ce#^wY!6Cv~7Mx|lSr!Cq1ZxCq1ZxCq1Z!^*tZideS0}!=*M8|w2mMhoN>G9llsGFfbaQ6g'
    'vF-XZv+c+Xr-^Zz7;|Bn3(H(s=E5=;mbtLZg=H=*b7476jMKyjjR}njjR}njjR}njjR}njjZdKQ{Z#(Ey59_L<JHa0eZLVvC{{38'
    '%$GgZCg0|!PuA1V2fsevO?SU`H7!=t>sM9uZ9l8*{r@JzlIA~t^474R`Q&F#+uELEH+9Zzr_P<sw6iCZ<#sD7*tWX+qTfy1I$1Lw'
    '3(Fra89yvIznk1QUp@@ykb+Os#Y5Y5+uyF%U-!#)?*o>@p6%kK`yF@d{$~E=*UU*c2xI*6Y&M^h;dBIMw=uho*=@{jV|E*}+nC+<'
    '<`;dyJbb3KQGybb_-#_!Zu-sa-$P$QOXu1;6Si;J&6%+KrOt%mmsB#H&Gc&|`&!Jt3NsvRhQrLaZ{6#B4gnp1j8$G-cORuW_+PQx'
    'tGo{F1XW%gLlqd5Mc%|H!+$Lylq4_Xvd`5Lqj~21k3AChRUQc$Q&TK5blSl+p>7{ICl247i;VGutD=oxF}iTzv#_T=i{pdK&V#Y{'
    '2R!m*H1xCM#~uy1KY`FPeHsTojp{f<k3J0gFzCae4}(4o`Y`Cjc+)-%LN!7)LN!7)LN!7)LN!7)LbW#q)y_C>!>QxeA31LQk>l2%'
    'b=-bOD%tCtxzB!JJZwKO{{6n({FE<OY%BoUV#c;;y)VIpr!U~KGuQnPXResXbIx4vUg69YL%G12d+4jB8+ZKOpt!(y1HK#Z-GJ`~'
    'd^g~`0pAVyZa_CKp&FqYp&FqYp&FqYp&FqYq1r3lxcStLn~vPL>Bx<new`cFT{;gqe%Cx;_o8{g@pK-rePSN4<2>Ns2EpeN6A?TS'
    'JP|w*JP|w*JP|w*Jm>v#rXwXNL5aU%-mg2ImMeI|%lp-tiNERlCjPn?P5hN{LW)e?c^=N3|FH|gzRCr0(Zt_ejrs0eF*!KwTo!w$'
    'z)N$*65cB5zBq7SltT9f-4}FU(0xJo1>F~PU(kKQTrom5LN!7)LN!7)LN!7)LN!9QzhKI*yL8HL{=O-{?uSkJ&5x%1+9#&`x}PxR'
    'H&@ZB?|0*3zVKyk+^w(nm$`ADUl8z3y&F_#_-?>=1HK#Z-GJ`~d^g~`0pAVi#wAoER3lU)R3lU)R3lU)R3lXTt*88s&rR4mWXC0U'
    'o>Rv;xZmad+?R0<E+I7`H6b-2H6b-2H6b-2HRs?`f)bSA9NgE>!Tq@}{eRM|*EgOn^|l-aoJv850UZW(7|>xrhXEZ1bQt_)=~9Gh'
    'gldFpgldFpgldFpgldFpf8MOupX)j0MAN?$j&t?`;W*(q;W*(q;W*(q;W#~~gldFpgldFpgldFpgldFpgle2<dd72Vo|tHAp7NZU'
    'BhRUMI}=Sgdw~*^pv2psy}&tFl%ND9e(yO~&(4;5i_@zJbvbBIf)bP<)TIO^D8XKfP?u1bW<m){P=ZjG5|p3>p)R2=p)MsTK?x2T'
    'gu0ZV1SJS{33Ul|DM1NJ5bCnmq68%<L8wcpOQ=f;N>GAOm%SDxC_xEAT|!+B8kC>}B?xu-oG3vFN)YN2>T=Ma1SKdzs7nb-P=dV{'
    'p)R2=&4dz^pah{VB`84&LR~^#LS0Hwf)X4w2z4nz2}%&^66zA_Qi2kcAk<~AMF~n!f>4)Gmr$1yl%NEmE_*FXP=XSF5Y&~c=syX{'
    '#1R4$?;mb~FKsuu?ice<{d(Tt>(39XhvoHjEigC9kHu>CpUUNK@_9PnT+RONXa7Mm`CzS=Tl;r<y=terThCYP`Q~f;F_k0rY#;9c'
    'D@!t_hLBR{N)Ex4TDpCiH!&pdis`Hm*qvQxqm3n-Vmlk6$s!R`XHAf@0I?)Tq*`M2Qi%!fY+*cCn9ddEbA{zxVLemm&ACF0mxj4K'
    '{NfCr$<1Q=Q5rAW&`rMH-ePQ#mUX6czLTqxn!W3M?0o5h?Lz26?xN|U@8s5F?UJ;Lm0NQ1zg~viN$}EAa`q@UkfaT=v{#nvE+SV&'
    'u2wmL)GZIoIq0hoozGn`og5ib7it$>7h@Momux3hq}*lGW#8r0WxZKB3!Z}1DsmQAk)&L9+ahPLa`uYb2yzxHT;xnq?#T&PIV0|+'
    ';1qJC94N=hVRDolWbrKJAS(x1eOgkvm&)bJtlYOvQn_4^%_0X?C?Z#-awC-+xiu@dhSu|FM7aoQG#BL}<g{`qZY6YboE#=cSvkte'
    'QC5z!a+KA_Cbi2^qHe-cdlEUQA8x&fT$S48={X~{%Y$=HY8S<D@|;|dtB?ki*6OfJp7(n>OpcO+tQ=(JAS-*U9As6Sq;fgP%0U+D'
    'oJ39<+h&m~B3DIDEW;V8+)CwEZV{T{<s#&fIZ+4~LkAt`uun`!o+2Kr9A)JwiygAZs+LRba+H;$tQ=+K`Pw9MS>%ey6_Kk_yOG+h'
    ')DC47J91ubQFPD+xe8GZ+)IajqB&NTGpf$u*;*;LYI{<*>=Ij*bHRG0Zn11DdSqeOMdYf~ZKQ4^b&DQ5(PJli>_m^9XrqH3chIs9'
    'd&C+NloS?_p_roDx!{~H*Lz{Pe$iwn%IHKJ9n`VIPT7|`+4HoP;*>8}d^wGrUUV25-eF%<^RBQuDSlQxamtsfe1{fCS0X!L;i4v1'
    'tvYt1&trOtGrmOCr5{IECB^7LQYV^>UL$v+#-hdJ>L@3CiHd7q7_^HjohY#Auc&XukRa7ULB}-_r@U0RJ2^yjS5#LtR}|NZ;#yJM'
    '7k0on<t1v}iMXh(Xss2k#n5U+Gi@7|Uef=neO#i|KeF$|uyUzt|Hz&f;Oh3v3?Ql)Y0Al$k^wg4jB*Jv`nY?OlRa1K9>cE;x*Z-o'
    'bWEPHJQ+0l395tgL0$3GaLwqSRUOeyp2(sNV1qh_XBY+^vQdR{C$boQBQ!v6UbGEOX7nLB=)|$Cs3HdFj3)>qGiuOI@jy6{1whEK'
    'c4)3qGjXs<jOX!0R-TR65VXOl@;M#s1FTB)<5*TS6FrB~N9mwEV&9^x@_fWDPH-;#kL%S!niy>_H<*1a#b>ECn5tTZ`labz4b{Yg'
    '-oUs3h}?g?6?m$TN18=+&`4{mT*^+KXAn@OJu$|sDj3lp?{&4dz2@zdtIrq?TJ`0A6WG33eeTy+i++0Z$Q4T>k^VmV=crdW3B!;`'
    'q`$ILc(}WhPGP-TZP+Q0NTgk-K%_t-k+f4l7Tf)1x_P)C;(43tyd{;*9@guAxw-1)XXqyPv&{mIGFxg0zElJewZwIk4FZzZCYMuo'
    'B@{V$dbfF4_gBa_v%zFvqU_Dt+q6qMJK0$*+;fjI*80WzIAv{qJ$dK5$*0?^<!Xu4HJfVorh03uoWJCnd}(=b^93+a%h7ADJ2{0-'
    'DZ>HUojuNDN9TPiDSFoxL<2m$)DbS<E;u7snmcb}fY;u3VQ~2U;ELTVDh{YP#u}1WZy9Q?L(M3GNAf$X&Mn56aHXwg+=kYi5UNNT'
    'JG-oAcX?~X7qy0a+#1#tpQAm~9O-4v;Vy5E+?qqi-4D~{e|wLe)o3h&0tf=6&|K7TNn3<M>JT(dwK?Sx(MWK~!UYNkdk)Qk9yI8H'
    '*aU(L2qxs(T-~9<jf_wNq5<&!=hQ+aF(4j-C?1p-uKHrt74P9sMzjq<3b@ouQkS^~dJ&LNe|FIrBLXfFo@c}l;Mj({zZ8x9S%Vk|'
    'LU!=e!!Hj%ywmtaH1PduWCD>(L@E)~(fBdK5Qriq(*lYJ!DP6r;r@raKgH(GN2JfeTa7Rs0;1WpfM?L4MRFRPz$4GG#U-Ip8kg2E'
    '0>TE_wFshIkURo}T@cYiWHsj&Ih8AtX8=_|m;)iF;#!0=YNP@|4Fn_*fGAmGkrsY=1Rvn`M})QnjT<I7=n-u}APaHSQZ-I04?x-<'
    'kr5~$!b}Ko%JmdD>Jh;}cnx7*1S6pl9<F@Eco7amG#1fUXheZiAMq7D03=BuOzJh7Cr<!TJ3N4FTTBx%Oat#eqHK5osk8{BMjYY)'
    '*Pxg_0pG%{upER$TKEq!t6W-eG-w<TL1@J5;E^xB#WLlR<S8Inj2IchX~ndN526=&NJc<(t3(a5W`r>i%0~1Ap{i;$BC3fD2sk5*'
    'f{+}dNOCz9K}rOM(fEj;Rj1J%X@6;c#KRH)Lc9y{EwAxFX@7*R(fld5Sd>>grA5*rX_0TmBDZ7a_AQkLN&}^Va>%4b(jsY*PYs!?'
    '#r4%}b^Qlqg~-h;`_J2(fk|p2B%TmnuwhT=BVJlBtiou@UQ38e_$iONrCNDa1}GDH7*%HUN0*%NsfuC3sLW~7S~@<_1r<v>%$M`c'
    '9P>Qelq9hZdFb9(v1jQ{QiZ1P%Ss8gR+#**ESl(ptGOJONv~L9h0zcB^mUg{pAYltgLgR;<KuIw^%=UpORe9YL;lU#n{;^IR85lJ'
    '5%#ew%O;0d4f=|Gne>oG#)o)V7Nk>(_UIfr=30t&>-#dfVEF-i+Lz&SLd)&kmK9T?bSL4k{fZcTGTQO&%YxXo*HK_!CKnu%rZ?`('
    'axA54Ugf^5y6l}MbMDHj+*Xe0ClAV83UYFzDXz^Q>SJ~Fur7Jha`PC@4$5+>A!`a{n`{PuWL8CNZoaBXqfygsZFii!51&`jT->1P'
    'o(7{Tl(cF-Vbs)5i{1vArs!;uf%xqF0_j|nj@iful>Sb03!j}IfULl%X`U8nLer&@Td18b5?igN3}($b^?(nW<my^iZ3YZrkq?Zd'
    '!m4Sou_X}OHt=b-vT1|A9fmMX!Hq-qvU>l}_5d21W;*oin%ZhxPpi4Krj0dz05O`2kG@*-%H4n$FtBNQ?=ZG&HnDBPtR^p;1~%d_'
    'VroXNW>9NN>o7I})@YvpFpg-7t8GcbnpSICc6?4l%BwnFwd2Y|@~v-~%bM-mphQb8wr#e6=F}U-HW-9$OGVTK<S@)BXd|Jf(`vSV'
    '8@JjV1KW_Drq`M_j<(G6QihobVaO~V(2z+(;_m=lG~wO0IRlzskBoH9vIY_|!+5Ug;(5rz_bu65v;NyS*|M>1%VXE%Z_TOIxdCxV'
    '__Zx}U$fl@@CB3rfEgQ15*0;?wbgXoI^+k(A=Tfu@!(0z(pg+^Bv(y3kKMCYrEUD8SvxPEbpxwAGF$-cXRQiWcVxJlt2=5?u(~7b'
    '9p(pIWJ_vwj|_Rp#J|XvbOy;IYi@5Im)X*GKFF~(4@u=0*^*k_BWoUJ%3Neisu7Q@xlIAM%$C&Z9$E8{YJZU}sntEQ=3#opMYg0?'
    '_sE(D1pmBAsntC)V+G$&8kWwC*c-0q+COJjEzA1Z@$kf~wo@;6BXS59Z8A*6bg2Z7S%te9nbIxitP*Wvrl_H`c6RmK!52Dy4vO1j'
    'w{s|NU-qj&+_nkj2C6f!0&vCrhJfuj0#2|-utu;(utu;(utu=<_Q2W=KCE_iv!34e1Z)Is1Z)Is1Z)Is1Z)Is1Z-~+uzf^=|1QCL'
    '`_Zs{X!9V9c{a*<DaiW0z8VH2;8(-=)i8cFj9(4oSHt+#Fn%?RUk&?{zZxdbE5`uW(f5BkEXa5f@dd84pAI{__c!=-*cKrV(<x5B'
    'sypV6{6q0Ezp6`oOngjyOngjyOngjyOnm(J;^WolWkbgOa{6I0EN(G;Y35dEA=%cH{g;gtZa)EXv?`m^FWcx`oqVu}o7*npCVzaI'
    '-_OyMb^m!CEfP~XS<($tpRUfFu>W$`&vbRSpZ38Ld<E#HU$0+ql9zwj1|J<-^jF(O@wTS{?t0N_T!}Jt)RMeD=kw?KJY8VfkqCcy'
    'KfT7Mj-;QRd|uBtx^Ri#U(Wvxt!uG+wfuOcnsK~C9^3lsanTyzO<MNXLl8oCF8b*`)CTVu{cJIvZ<kE@BuA}=ZyT-GtL@j09v1R`'
    'c746pby;>L`lf;f_@wrY=;m_PUrlulmG|$xrdEi}MXw`3EehC%|FyF^mJo~fz(B+Q+C`1lkw)vtqji+gI_hX0(vV}7SUX;e8?R-&'
    'UF~?o+VPgP<4tSF+t!XZt{rb(H{QB#ymj4p>$>sQb>pq;##`5ox2_v+T{qskZoGB<c<cJ{*7f79>&IKykGHNLZ(Tp$x_-QM{aEW_'
    'oLD`qTc-yIGj>)^m*sR>PM2ju9~1hR(8q*6CiF3(@3&0o8=NDYeZkomoP9yCMzBV(MzBV(MzHo4!P+)%b#>x<d+iqhb<iIbqXZ=='
    'L5Z^xLpNu(9ow!yGuw{LaQYahk1;)#>9I_YWqK^rW0@Yy^jN0HGCh{l$2fhA_?Y;Z_?Y;Z_?Y;Z_?Y;Z`1nbDd_R>xukJU419^3G'
    'bKhf97a-zdzU;A<`8I2PvYvjPyxXS13znYle(h>ntftqms_5H(uG#zlO@<}Vf5vpKVFC2X&z;D%J<D!Vo!L&RGskoA?8!8`-MS05'
    'tq8y9caykI)|tm*^oPsL4~x_flkt|D`SM{nhZKC8E*{#h+x~X7{<>e>dmp|W_H36h-S4<t_c!w|zh;KQK~UqD=eGHr3@0Zr*NwSu'
    '%yna~8*|;5>&9HSH@`FlX5}-%jS`fg#BY<}cGGWW{~q}hG`VZ%P#E5}n?qsy&fOde!!N01I-BXYN%plI_$175uo(_B-@bLP^*aP~'
    '{V`T~b=`ifMh4t=K`6mE@Ji1^J4Kb($4~_dWjVscC=j5Q5K594a@pr<iP1cB1&_TF_Ela98CO#*GJM*>J)!O&I4BO^o(n!<)7%wp'
    '42#i)1HXkm^;;YtTy|cJy-(ngH=`k-9Y6MJ=*W&Cn|_T0zeaVOvqwJ${TTFP(2qes2K^ZHW4viU2B8|E8lf7Y8lf7Y8lf7Y8ll>o'
    'f@)`+x8c-zTaTQ#{>XXj&pL0vBc<%M4&7%zJ|4CoAAgNMH$Uai6*~)nx0uH*n(s>xm3{IO9y@g14{_*<i9F}f#rNI6+o3DQa)Cql'
    '&|gbe?)dvbae?m#d_Um(0pAb!e!%wwz8~=YfUaCZH9|E)H9|E)H9|E)H9|E)wO6=u^NA~0zq+`0<)$N7Zu)huTzC1L;P_p0g53+}'
    '1jo}k!S;zc!H#o+e;X8^OH4%ZMDaxNMDaxNMDaxNMDd&h%vq0=padoUhB?6Q@dRB#6kgu2&P)PM-!}=^y>Jq+j1^L3Wbe~g@YoGu'
    'U*(3lXcBO)#(a0qm>e8-Zi~HR;F0?RxN_LeXN@JiThfVf;KV3}P7FFR=)|BCgH8-OG3dmg6N6b}gldFpgldFpgldFpgldFpglcbh'
    '0<hSYK!$DBSo`$@yt&U#0M6ex0oeV(3BdW$1YrBb1Yq|QCIHKolvg-&F?)F2nS1E3{bjD)=hq8-Q||}W8NMIz{ebTWd_Um(0pAb!'
    'e!%wwx^fBC2-OJH2-OJH2-OJH2-OJH-tGk83tYM5vlO;2*>TC8=hSgF?ss`V_hp=oOGr&fO-M~hO-M~hO-M~h&Dpq=padm28~63I'
    'aewYh|DQ7V^*2bDdRq<yPN$&5fDQvX4CpYR!+;J0It>1@bSXkLLN!7)LN!7)LN!7)LN!9QKX2yi&-I*gqUqlW$2og}aGY?QaGY?Q'
    'aGY?QaGah~LN!7)LN!7)LN!7)LN!7)LN!h_J>xkwPfRp5PkBzwk>}LBor$KLy+8>{P~z>+Uf`T7N>G9lzxSN0XJ<>j#pzXqx*Rkp'
    'K?zC_>QaIdlwhw#s7t6zGob_}C_$)82})3cP?u1bP?r*vpacgELS0Hwf)a$fgt~;fl%ND92zA+OQGybbAk-z)CDf$^B`86t%U+8T'
    'l%NEmE}<?54N6dg5`?;ZPL!YoB?xs1bvbBIf)bP<)TIO^D8XKfP?u1bW<m){P=ZjG5|p3>p)R2=p)MsTK?x2Tgu0ZV1SJS{33Ul|'
    'DM1NJ5bCnmq68%<L8wcpOQ=f;N>GAOm%SDxC_#xo2<pmJ^q&M};s}9>_Yb$gm$sW+_lx<bem(E+_2-Ax!}5B%7MPpl$6_`6Z;<;u'
    'oo}vY|Ms*0AXrT{KFgi`JH1}DGu^G{tMz>Iwf&gNfqJr!cR-aT8B;?@xpO6lU`nmszRa5#l6S>))(7m)uCvj`l1;Il4N=Y(vz0;<'
    'q%1%z$q}iRSiMwYf;(Fn&lRR~h51}zIagTE6nb;6(Bhq8F3-L=gGX|+n0}PTi#BwVueY}tTC6mP>74K6s-$M`Iv+b<x?sByx{$kQ'
    'y6C%@x>&m;4PfP#ocynsA$JnIw3M7Z$_*rGgDmZp<+_W=Rgsf+c1G&9ata4^^`Vmo7fdHdhSY`HMc2jH#nL6)Nfq)uvgxw#a_X|)'
    'tegc;L24B_i>pXdF1u}!vsXEL#cc#Rixn<%rYQI1gsYqp_fl{QIZ_Uk<K!?oN)EDkmU57lgRDL+soYEDa%EQT+a{@8F34t)gDMn}'
    't5Ug<%8lHbm0LsWc{HM2gfyCqauIS`ITW`NIyp`blcTJG21)I5l$E2bJ~pXcjuLefmfDlZLH%&+MdYf~E>F)Hsa+nNb5gq~hLh*y'
    'f?S0(ptM$pUGlu&XXte&2U$7D%0X84SUJe5Hc91jkd=e1JX4!QP8!>0kt-rsMXpNaMk*I|cUEo@n&IUl<dHd12p2;K9q6zxb+RYm'
    'vC2_aj<VPxd#q}?)GkL^Im*gWR-Ug-B9}$3h+Gl5DzzJ_-Ae6HMzJI3<rYN;U688~<-omk*e9A}RXL;T44$o(a;vr{b;~ZXRXG=|'
    'SL#l34WdUDc3niSO5H~4Hd43fu@gOZqQ_43*oihe=y3-v>##?xAwfxD0U3%Zs+|kY`EtD%mg^TycA|_<w9!EwJM5Hw(gB6;X)VPm'
    'U#|Fa8ach_Ff_cwzNqG1VRcgcta{><FID*tEsm~4cEG|#O{`jV>_nf(^b%)$iK<IKj;>0I(SxK;G#R}{?nI46i^tVbPWTcP*S;`l'
    '7gah@V9{SuU(sGG+H0Yp<C=(5UaH%j93r|asw<i+ifcu2ttjpbJ7Aph61DC`T+~*y){53zQA{hEY1^>$lKxli;}Wg@k$o?Ql}lCo'
    'NA|qHR<~bfz);0VQ%=5=46q?*luL-w$K8vZ?73R^7=C5Y?eO5CWAcpU$)M3sKpm71>WZg^YexU9>WFUgL>6rT8`Loz07oCPQH63R'
    'vKW0MG(c`%v<*#W^dULu#IdZXA_nM;CkP`mYS2#cKsb>FK*+FmXs%H+aj;2@=kY{Vo{iWLw85zIIUVc+tV;CbSXML>J%`ap>7YDf'
    '-=eGXe8etJU@qK`>(xS<Se{pop~47!_OTS7rRrd+suoTn?eA)+CKhxF#s$FS{^PAcRDDK(KsCe~n_S8ce+shF?l_&_>uPO#&D$wg'
    'zcHM&>d*Zq(0#G`+^??|{q*LMJC;Nu{eAS$QLk_kiXo9me`TleaCax2!g{sZuu~wBNV`sfNP$EmX{Uhj-u>!fJ?r-_1$79L$z<{`'
    'X7%-J@X71nW%vsLCAJTlOeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmd'
    'CX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<`6u%~-Q<4tu%7i-)0<7dp1ga{Mj?~QZ-K4PovpsJ#<#!I@MrA|'
    '8;DFMlgVT<nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmd'
    'CX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOojv-zKHay{KluRioQt93tl_ui)1k;!B-nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<'
    'nM@{=$z(E_OeT}bWHOmdCX>lzGMP;NwOkE1Zrr$W<Hn5}H*VaxapT5~8#iv;xN+mgjT<*^+_-V$#*G^{Zrr$W<Hn5}H*VaxapT7A'
    'jc?oa0H-&be$C|o$z(EF*$Da2+051;lm8~B<&(*OAv>}BRJv%{8f5Zc#GYe5nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<nM@{='
    '$z(E_OeT}L>>oF7+<yFKb`#dv7To@}%~JchapT7AFWfwPe{S5kapT5~8#iv;xN+mgjT<*^+_-V$#*G^{Zrr$W<Hn5}H*SC5f`6Oo'
    'hedyX^`XC6t^3Kl_brLobh-1cGb9p;L?V$$Boc{4B9TZW5{X12kw_#Gi9{lieoM;2N1=bs1PFyqLn4t#Boc{4B9TZW5{X12kw_#G'
    'i9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF>rr=$~$~nSNOG_gB-KO~0PJ'
    'd%xY~>+#Y6Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V%XC$h$OHXTR*Q6>eUuzg4*5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW'
    '5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4dI|mW?f(yfY>KM'
)


def produced_address16():
    import base64
    import json
    import zlib
    return json.loads(zlib.decompress(base64.b85decode(_FOCUS_ADDRESS16)))


# Actual terminal-CS report, audited with all29 real compile/delivery artifacts.
# i4v-address-concurrent-dev-20261026-04/address-cs-boundary-n4-actor1
# Original command.stdout SHA256: a5cd0e391e18853b81c1b9d99ea226f9fe1b937114404b3210bc9b2f1628e1f2
_FOCUS_TERMINAL = (
    'c-rl~>2lk;(lvZ9`pvHjBLU=AQYn=p)7g5qEcr-shWAwN{p;1BM$0zk#3y#Vs@ZWwf*^@;p$GJTN7Ko=8+{l}uC7+ydj0=Rmy7A$'
    'YSk?^qh@rwT3*lQayd35xo|!Ea<}TP#$Ttmck9uI(2O?SdUJGQGTkg!qYt(jeZ3hkBrmzy`qZXiN{nr3U2;AkVSV1{!+%E$z4P!{'
    '`&_2Wn_D@1HTqzh(Q<w@zFIDjr6gJ}?^e@pJpE^~_<{r$_x-zAev{;1x*Pd|bxpYFz8&0lGnp+m-C`oIHePQgt4)9Jrd!?27L)mS'
    ')lIH`)U>;E``dS?{&Uye>8<|s<mkgk*O<mP=F`EYdAIno`DgUuF!?|7sN*y1ZuNCGoh`md#yJEjw_uV_C_LNZ`KxZ;NsV3Aw^?=5'
    '`DAvp{n;1E=k9vB!fQy$H@5gj|62J|8rL`zz@K)>C)e`$)70gm)bjZ2-4ncxzQl5TvCmJg1>@x7t|ptw>1`oqZx1i+ZVE-OOqW*&'
    'AM(-3Ip2iFCL{0W<o_i$&IT#x%Lw`4b|>{_i+o{pjg9!)_IVlGHfSc!dClbb0hed^so}@+W8ePAe(L-1c69k@nm0{Sd;9`^!LNS7'
    'AtcyLKF_=LewN#H?t;E&NF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{m(6=aQvDL}|93lfP$B9Z?3Ip4ON^GPHUi9{liNF)-8L?V$$'
    'Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4`pxvI8Ev}N&1^B5kFO@1$@p`3'
    'y<Byp4<BtC8#0+pCjVK+Q0+rw6HDVA*1Szw{%=_x?~RLVQ0r&{EROfZ?Dp@L9Vh#DT=VSp?~^@y{X4FC_IfgzOeT}bWHOmdCX>lz'
    'GMP*!lgVT<nM@{=$z(E_OeT}bWHOn2j*H>Jg9nc%4)~v%>VL|@_}?EOXPs07$7~+&aQI>S|Kr2I<M98d4#(#I$D6-r^Z!$aXY=R5'
    'g9i^DJb3Wn!Gi}69z1yP;K73j4<0;t@ZiCN2M-=Rc<?xLd}>DP<=tx9jlZpCn{M>szoX^#^}5@PK4e&0jOMdNH(8B71ZP~5TSlwN'
    'x6uc0V{kS!qc69U-LIcNo6&T+TwTo;lg)C4yly9}&1^cmoh&wM$wMAC?G}^OY&rTMc~9oE$$EUdT3*lQ@@UC?y_rn^JvwtU``%rh'
    'IWt|}+{&3NJ+<m4S3gD{tmJt;Tg=w~;5>fKm(zboA6g$wDy}r6MfZI(mN)trAGkk#`_E)uGoMU1v#<5P|GDe#w)y-c5776YcZ)BZ'
    'f9j`ntFQ8Hi!YKShtQ@V+4*EjjJCy1c*S|Q+2*n8HWMk#`o>?nMYo!4X3NFsgTrJ0T`a%J=X~jIIxQ=Mm;E-DAFEln|9y~We`eKP'
    'cdKqO?GvrK>3lM~(T6sp<@{>g&1YX`pZmhQz56`x%g1SP&pXNCcD{W^J#+ZfRd=`U#-CA={Zluq$zna%mmhzd%r@JT(ulUj6OHc+'
    'aP;A$TJAJfnp*c&avtAyiz_X*kFX~c+Fe;JH?!;6RI^?mUDxb!CZ@*4RyYfn#zx<I{nzThPXC3Gy3et(DVVaoDrXYTSi8MT&IC=9'
    '5AJF;joIebw!t^iM*W}Fe~tXESDgGW2>T|rl85w=#^$z2vedR|_n2JdC2|RPd~tHdWslSWsdLuUVfPBTw3<3-YJ=1Usawt4XzF(N'
    'dbwmx-D+xw)DEem=3O*(+!aACNmEBnZIRkG&IEaalg69%-=hB}{Z~rb6d4Dcbx~5<Xp)`@ddBFPn#SQw$a*H|8LwxGo{0xFWe#e}'
    'K2TFn*USCdM9rKlP_4H!g@O7zSEQ)%^VN7<c(L_p8V==>`r<vVI=N`{qB+wF<f7M${!BGW-e_3PX$twB<+Eazcar}V{LeJ<z4A{h'
    'Jwg6y@lV1(rIA;UxaxQ3a?PM#)x^cRnxCYzy9-BI6=~OU)$Pue`tW_Gk|gY2qra`bTA$VVcX^T5NJ09OK78M1PFvAKE#$P`q&J&~'
    '3a~x?E_;-u*!v%cSM6W_=+k!}cT}0>xH9);?2q3dohjp|-sP8HL&`YT*Lb+^KGfSzdykZ{-Ia0wT}So0|1S6RcbR+b?p_13yS9@b'
    'F5~;Jk)QD{zw#PV#{C=YN4ooSk{dJ`NEy3>?|Q6%ny0_Z9^7{N`iGsgoG-=WG9LOaFY+2v#%VuR_y=YaKWH+SGWKVO;)7B=?4S9t'
    'f#2(z4-S9+(OwyT*P!RTk?+R8PXD#~uQ`XXb|u$tAG<3>bMOl8;6>c_b+n|Nk1-sTH_kZyHy({G&N~@BoX^$_YWFU9{OdE`VZf&&'
    'z0v&bpgKo-{A=VL@Nd+AgZ`U7$uj6hkp4ziI*X|;sTR6mI9R2~+F>@C%gU+Ut}V=dZ2_3RgUI`^?CZPFH?xgG|Bt0n`gF9&I5^wg'
    'E~k={0J<|v3%tN)x!FGd^JFvqr(aIU>Zq>YRzK?cVgKXo5(i(o`Z}3cux>%0_IHi@r(i8KU#{y?>1c`8jIQov@gayG%aH%;rakh<'
    'd)-XF&u;E+##h~Kzv7yAlk4$6v$ZTPe_&l<LrhXj^0#EEC;mSA-8YWq8q2cDHJ0f?V0x0%cPm*jZqPEPw|5WK?Y51YpZld;e}g$_'
    '!Tx8n&F0{2*qwC;XXEayKkNfPKcRETGuE4f>b?Igd+v63_W9=?zkjELJkP#TdoIs(F3)`K_HyoaYtG$o?YWn-?p(I^Y*E{@MQ!8R'
    'eA~16p8YbL&*pIU+nhODGxl7*&YjEGo&7c!&gOge+uYeob7#NJovk)k&gJXP*#>s@+x*#Y`#oJmdg1chqFb%CYpgeu&E2|=ppYxg'
    '^@)AQ9(VIz*tVIkAjrd1=LH5qyoEFJz|C}p*4H{2F<h2)`)E43-Q2CZ@$D3i3etcmS5xw~-3q%-FJ@Vw%AMVMrH{X!)W6-l{$1bp'
    '7iM*W`+758EOkn>ULG4T*0e?PabBk4Q7_V4jr*|xU-}VG5O9#%g30B~ZAPaZFF?Q)fdN_F^js543<#jAa?6ZC5i&|74VX=Td%o`Z'
    '`3B_cO7y#IOP{Tb=}pFz`kZWAWZOo&%~eoM%{9uaW=PeB<?4nPLcF99-2Fm`#@I83&|XpqZfGGSErd@1K#PCJpS$bj3R9ewRYltr'
    'fqfoO5#lZYJIO)bPtcVhM=uwXO3dVfToiEc8bCx301BZFAf;RsfF2=?lPkI$Ztyh_n|WCZKwv0W0g4lZWDsh4gnUi%aR9DZDrX3Q'
    '3PQmq!TF6v3l0FaQv447sDh@Gz)}lv3-Bu6g!gF#O+##m@UZ~d1@E5)9STN<@DXxFSwx`RorfGy>a+ophj<bgS@AY9Z3K~HG3pfN'
    '%SQt!V=3PVNJl_=SAgE-f+FGuf-!lJyt@Z9hcFQ!85bK_LIA0I;Ccrcp`4V9vY3%48Pq9|x=I@xNDie2fWoB$ETjrarHfKbsN*I<'
    'Dk@ij;vvPfkO;>{>OiW%Vx<7QF6$EKv{e*Y;mCQ&AhC*&?}NZdnnfeUFC!t)zI?ZQfkV@f(uX9~VjV8mimr;}cO%7*g$ZP>5I{?V'
    'aA*(^*ELvaK=g`LNx`}W4Wf|>C@O2ML7fejCMcIiYCtN$dX<mJ39KV<v5^{pxJ2XsLO?4Q(cxt22f?vH<Hypd(UAv2SF8->3f=>Y'
    'Bl$RshH16=%UenvSY6PnJW{Smf0Hf%QL$Xb!bw_%L#t?{4x|b!MBD+(9{E0bqtHkVASf+F;Su5o`2u;N)JXYD@mq`KUaXr0ph=iU'
    'iXW>&h?%8DK%3;yB)oL<1`9>3TCu2<CgIQ|WUcPi(HdfOtfnv+phd7pvPZH<zMdYroiiubvij$g$qvd6$|;jQl0A|=@{uVMwm8$}'
    'Rks)Fzg7CQwMEj8LDxgkVl=6HB&oefxHw<0743C=(XJc&)`<rAoQ$>np-{qjAekIL;PMPVLE$mms7UU=qwhdIUiw_}zDPHfZqXKu'
    'I{R}uhf)ff?(SSP(Fa%12>0itbLUvln={1ls-7|-SEHhkRb>{uOCcL=d_jfvC>?2($)M$iXp#x4(!!l49ON(mrsPd~_{_GLR>nvS'
    'ySsDAq!5e22)aKfqn?pLE*_o>GE_%<^o$f!$yrCH{W*DI`GG-Ze-5t`bhOx=%O*z|BEn(WWd~iIcHaHDAf4N5Kir>_7Y;3^xPx;E'
    'N?%M>2j_}Q9>sicuE=MlXv^Vu6xRlMcpZeQnA6AN%3)qo%kuFUkq^$LwuB_3t2C1TsNhn%1crT#6&RN>(qNXA0Tk^pU^0|1mhmjK'
    'kZoaF!1N;Hh78d%7^A@!jO;RK%IGM=pp17={K|ad<qjEbWR#NuP3l}mDH)Gs<blGwU?!I_TIyTIT^V4dk~73|(iI}?H!y`s*N`%k'
    'E}Njj#_S;zg_MpA{xZx<k(4BNNYj;OCL?KrGz}x6G*TFjdSD2k1>gc80L=B6*KLs7qy@>yDm@H)b>%uMqP1YALy4eBP$rm{)MhQ#'
    'iN&jw4`q*`9)mgta17QMnAKh_)R7gln6JHBVF>^Tphn0lCo~lUV7kXJuAL^<nHh61dI{#$XzF~4xe)`6Dw88}z|>TGU7c9a4s|$+'
    'bskWYmY~o|$34u}N!8DQJIFy*+o|G^s^C^20n7k&033iBA9FkAajQGbd7X-7)<q70P=HJ5qkutK4H9(H!svy$NT(fRE4%}e184(0'
    'v$aiS1x?z*F`xqW0O3@Vy^2tPMbP-r`q2DbQj8#QKyuL891}ODXiUbKcA+YV`rEZSNn$?4)Cb(D5EEz;z%ioD0pJ150k~nN03HMM'
    'Qk12Ds#TPv<8*{jIYQcthM@QYPy(m{_yA&D6zhXMOK!qOandpY2GMRF>s4CDjVk_uO5{^8J3Q8_(CdymE&%7Fy$ZeVsN(|SJld<!'
    '>yA1upzhNh3cc>A_g08CtS@P=yVsE`@E+Eeki-4nT*2S4zNEeGUT>}of?<70d)>X>tf+KgU(yzFuQ%&LWnf>@UU#oISGYW^FKMs4'
    '*PC?#HLx#fue;Zq6~{l-rL@=G>&CjKd$42So`a5yMZ}}s3Ptq9e0b8Wprl#f9w;6|uqcz!;{Q#*Li3x0k0G{DLEfrfvrQ>!ifdIt'
    'N;8zwPEhcUI+(1$_~c~}oOd|_i=ly^K)Az8e+a^D3s7EPP0Qaqd^=Wi6S@()5xNn&5xNn&5xTuT=yp9@Oy=Y3)#Rold?S1#d?S1#'
    'd?S1#d?S1#e0%-y?Tb42Yz?jdJNj}9<FNyk=YfjzRQov;aD_IszAb`FyeYw`XXO2p@!ZMw+{twAWIlJYoH?m;<-L<|kz37fb%zxv'
    '|Gl11zN|+du$9E<$IXpGn9D}TI=Z;tj>Y$860tXrX+z%&tI196b-hW8RfCDkN7p*Ogx-)R_SPb8H4xH1UV8f*(;Il$YSdHbEcBG1'
    'Rwz;Jj#{;A$!a-KdSe;e+he%i3di?09<ev>X{+@}oyvRD4AUFr*lO=lJK)y9$*RvotL8XK?LBg}@iDyxiS12?d~cf)dV8c;ZC>i2'
    'SFK6Zl*aVtQ?@tHan%N-c5~P}ArHAMPfco=RC>b~(;GXv-k`?!CRCxf(`kK=ZK~~09sPRS8teKIshaH65vUr&L>(du8jXihl{~lf'
    '23oea0&>0OSLiK^VsF0ER-2_d=Jp0brZ)n!)yk-jd%Y1))L2+X$D5$Th#DKA%_xGhy=jc^?V&<%a1*PYm5%e-^!Tf{k}<tylym)v'
    '+?!mX9b<rtI&KvJiJ<m3QEjd;Vdw1}c3HdcN9o=)E>znql#uqmgt77AR;rWU#SOns6;TaltPbW;ySEy(MKzjwxOwU<d0~^s#DS)w'
    'C`=8Vq8higYB}{(yTlVOY3e$us6ARx6U0_c+>#nmm1;Qmcyq*@c0m)@u~2RDTDADfYUfvaGcwy74?f-!b?P11wgDoOS|#Rc_GWrJ'
    '$IAv_ow)}zYpe@PHOn-;-Jk1i%Pt##wSNq0)xUG>#k4ZCY5&fV7pUdt`xN*i!!3XGJtvt}vpi8=Ax0l}_FQshyz0JGSHnhaQ5y^a'
    'FrHNVza#Unk5*0p)J|0wKx#s2)eO?vleD@+fZ{B*4s~ka=(lEW$7#J<GGc|4(ArRBMk~poO&q6%P@+Y<ml{MTGuok@;(>6I761V|'
    'QE1CG+D)7;g#!BjaayUHHfw7z+W4GK_CZ%m?MYf`nHV{YR;AN*QcxL2i+*vKR=v;{o7Hj-tS6m328fO1Lw(HIC&@aPQnzwYq$@}&'
    '6ccmMO|Jp~X!oVxx!~hRK%f$0i8Wm-$Ws0*$SPHEd-kfUt9s=BYR;?6JG^RjQ?(sB?p-Rj;P^o1_=sfkuSLh1=$Po3=$Po3=$Po3'
    '=$Po3==hzYW8L}&xVT$PKF|BT!}@JyZqyIS)}rjbZ?NM|zXQnWKFhnkoSM<?YWZa~5j=_AZ8qJlUjI5<&$e5nZ8y`=a}a?yBf;i3'
    'utBJs?Z{)tXCB#ozwM@VL$;ULuml_aTz9M0@CRJJi_O8t&_52IlFVgKkrj(f8@e}{dGJx+Ci8#S*o?bwkcYyTAz`H3k>iKmzs>*k'
    '<!W(xx%e{HR&jg*TUY-$KDFI=yH`EP#d)Xuef!hxvcJkr%YF}|)oQuj)#z?63$v>$X_4!LGrCAIM|=H^C)0nmBl_LiEb3l?5u38L'
    'Ci~rA9jnfyiUvdZ_OFgogYs$z<&_5Il?UZj2IU2$Fd#E)hvnvm<u<JR?XU{4!)m||s{%W$4(zZ>u)}J>4XXt=tQOp`T5!W^!40bg'
    'H>?)iuv&1#YQYVw1vjh~{IFW^!)n0~s|7!-7W}YU@WX1s532<~tQP!`TG&Ujm>}lE$3dk-e_s0Y(w~=Bg|sT9RUxekX;nz8!Z&AC'
    'c(9o{9YW|3LWdASH$pc;H$pc;H$u194c)ftwDAi|;fUdg;fUdg;fUdg;fUdg;fUer)Jvycx-HOcfo=<QTcFzl-4^J!K(__DEzqf#'
    'PQ66OM8`zOM8`zOM8`zOM8`zOFNcoT6Z!9Wz3D~A%j@fP2T$7`0gvXhMK_Tay>;LjttQ__AGYp0f~F_CU%TAq%gNPgW`Na4nR)d#'
    '-DV~wd61d>`uTJ33H<TD^3wkSjDG=lTig4<zNn+^_8~imXR~#9Hpl+a_Qb(?7b!<wt8Z@S-OkB&y>*SPe{ZKX*W3O)DtR{TdN=9C'
    'X12KNpCKo{PUd&@(oJ`>T>aQ74?gb0EcREn0jBpCZdcv)?EC-TUF+b=A2$<`c0n@{nu*X%gk~Z%6QP+1%|vJ>^7=#fV9S&(Z2Q&V'
    '<2utnnT#ozf+?8dY>IwtGuzQ@H;9?-=mi1Ab+?)Rb0|%y#$8)c!gi&-6D8<+dnZc3FUe#wo$AI>`&<r08>YY6^tYLxKbv2aI0Tgl'
    '6q!7>0*BJe0tAO)viLQDgJxfHkacmC39XH(^=*|iKJ>Kvk_Y=sk_VYT+n8mVw7m#oCozH`;%KE@+O1fk&R0?1`9MBlPvsMbH)pe#'
    'R_xdR_oNmT?d<rx^a6-1QGT(l6-W66<rkD+P<}!A1?3l%Ur>JWrsNldZG>%vZG>%vZG>%vZG>%vZLb!#oe|)MQvq%{65#qH0j@tQ'
    'z<ts}t?pgD%}9U!x?uN_&=8>y4e<+dUCmv(Q~;`l%eFLfK)$dQ?4EhteZj8#ErMMb<n08zYQ1+V*v%@&JrL}+QCj#=!LBsJonSY~'
    'uviji=Ck%_I^4AOyi|98@rC8O`<HyxD}?O0g*aCTTp@6Uz!d^l2wWj>g+RgX8xZUgwh^`wwh^`wwh^`wwh^|yQNeC|BG@hVNU$4^'
    '1iSG;!S18p!l6Ah@zr0?5$w9*KEm-iKEm!oAK`fFBWxe=5q9(uenb{~aOux)CZA(VR8Le-R8Le-R8Le-R8Le-FJbycG6hpG#XIy8'
    'cK6+QqcQf3mvH+WFJbqvm#|L;H_K$_&v*&9r(VML0WV?qLSDl1GVa-4!byFCdwIo9d~_<c0D$a|`;rT^7Jw%TFAhpIX9_PUyrA%c'
    '!V3y7D7>KXg2IdUZAnJpM&L%^M&L%^M&L%^M&L%^_U4_0&xNu0Wx4JnPQvLqPQvclPQvNbN!UK%B<x<uNm$;?JljcFKWZ!2JqXix'
    ')*#Jkgr*0!e%xPtS=sI*u>@b&5&_l1B?6ZSTq1CZz$F5g2wWm?i9p%z8<6c1xDmJ!xDmJ!xDmJ!xDmL~Px!*9!iTcmryUr!a@wa;'
    '?nJDPZo8LDM-MO2ZI>XMAe$hYAe$hYAe$hYAe(NxOu-aPLATxC?za1~B7ncgd-ljd_Rl%bz8w()eK06Opa_8?1d0$SLZAqNB7}Et'
    'U`60Y;6~s^;6~s^;6~s^;6~u~9-L=iR=P?b)_0B6=?X!lPNYtxPNYtxPNYtxP7y1C8-W{v8-W{v8-W{v8-W{v8+}-xC1UmTVWkf%'
    'K{i1)K{i1)K{i1)K{i1)eOQ@-DVTyjtna~x)zgQSt`KyEpa_8?1d0$SLZAqNA_R&MXkbO)M&L%^M&L%^M&L%^M&L%^MjzH+k*?B*'
    'm97v(>O|^9>O|^9>O|^9>O|@ku@bluxDmJ!xDmJ!xDmJ!xDmL~hxJksYfB$i`mhpY6J!%)6J!%)6J!%)6J*nel_{8lDd@xc9(-6^'
    '`moX!g02u0Ay9-s5duXB6d_QAKoJ5BtO(o)+z8wV+z8wV+z8wV+z8z0!}=@IRr;{f6@o~eNS#QXNS#QXNS#QXNSz{90yhFT0yhFT'
    '0yhFT0yhFT0yp}wUMgZW5BRW}heWLANW^Mhj}I$dA((<GnBw)jLePPWDVTyO-nIkRqpnx4(N~L5my-rlFa=W(>M{jWFa<{~LR~^#'
    'b`z#x3Z@{`WeTQX3PN2%T|!-^U<#(-q(P|56imSsgt~;fgt|<@6ih*=%TbFdn1U$?bqRF|b(w-Gn1WE3qZU&z1ylS*P*>if`zk0C'
    'cL+>e-`(^(%U*T!+1GA0>(=`7^YU(SHCg@8O=~~Tm(zdybl)bk&3O7xH~kk0Q*jYt&es1-u9o#lx2xH5HQW5CKPGacKJ3daQ02jP'
    'UM^XeO41F1PB#4-ywg``L+jg;WAUa06WrO8@!ZMw+{twAWIlJYoH?nRG~GLCv0|9r>c$&c{(C*2e37DyHZ-FjH#fR>)@9rB)NO5a'
    '>zPuwgu+%cy17%T+cfF+FS%~yqT9Wcx^W9OS8?X@ZGGy-HoE;yt{d6tW;3O38-vYdbOV@FH)hfeUvk~LMK?>r7A^Rej7MOzu7Ygk'
    'Hf#cv>efEG2T!iM>FAz2rS6`C9d2|VoK$zT(LJQH?qE}QnJIO@7?hsw{F3TcF4*Y>dkY!;l%N|AW$Y(Zcm64LM=tF2qt%w`&OW;P'
    'POkgt=x#fu?weD5zXMLHd)erIGr8^qrTfbi>Ep;lF3VG~y;zI=jq0XEsctW%n-Jx?ose$%Q|jhD*y2aG;YoFSEZue|*9~-Zo1IcO'
    '%)zEMy7f)cO>Cm>O;gZl<Vrxb>Rw5)-}<Q3O^m$nw<Ah*V<FvsDA$dIbhDqL+xF<@KDq%<s$1#khC8`#og<CI>kd>XT;0DWW1BX#'
    '8Qr=|H%<!u7D%~nW~5slmAZKmHW<=vh>~t7RJZWTbu%vA<fqi_da$Vv+A((4iMnA<!46h(8a*JPVPdDKyxlzMvUcBZ4;1?SezI=B'
    'g%Z-<m#`_%!>v?zTe`U6*FB1)4F}yS2#?h6UC`^~^rOvFw`aPr$z#VNG!@<cD`EJ+eq0&*<6#f3C)y>Rcu7;&-Hf7cFIF%-NlV74'
    'k+8=XHs*S=Ibt)V3!1o&g+Yg?j19%GhhM?ge7f<M?hN*1OVmA|2DWYNUM6i9o027LGgdbu%a;zox@FXWW{nLAvFTZ<o0wt0H{Fgb'
    'T{`}1{}|G$f9KeXX=P~B{+%N)kk!riDG*dPQk0|bImxV=<%#kNG5WY$#>tiOs>Ae)4J%RcXk${xQZp#@6G(@~=XHaxs5|dreAecO'
    'Ve%v`${^?lUoA=<uUNDbT6Fm1w3vOPZq1j_+E8RhE6Jfv9H+%@m@V49)F3*U(GKkt4}_Dn00;@a9WB>rH*vZLUqJsqPAhdIeF&|='
    'XybD_*$3UGuRTdCEfXV$(W-R1L0{0#Gh6hF6Oaqx<7zpVB9{6}F<`$^`A{Eo_DQl1rfAc`L!|s&3B|;WA;Gu+klcN_5rC?71PD|@'
    'EU~6*lf!=nS*5zG+S#kFZYPQSU(I=y8bd^@(%hc{+~>=0-D*7VCfE0bu_O}d{n4kRQQ<@pLn4vh<)CnPdn<#&YPsBSP#}>=yFr0S'
    'fkYzdpa46z^=7iUTlX5h&16>1%BFX#RkzrTJNX%!(R#X>Lt18Y3Be=4=A@oMAWGd(-nK5a$z?=~oK9{xcdKrE6kM_ff7zLxom|Wo'
    'GP-*<Yu$WxY_vA3I>oOy<Hd5J0V%yaHkc9-RLCXgL#T#uvpFD8HPWl%J9&a_+XNwKbM7{))V8s0n+$D$hm<-(<lEGp?d|R_;sI2F'
    '-*3#+Ju}mQOkIh7m#5$Vg$Vf>dA7*2RRzIzkN@r8AX&PhS-OGW9xwTJck=Dg7<=a1+e^OP4f^(U*|%FnUPzFDKJockKVN3)&y&T!'
    'd-<Ntkk1xZ)e~)7ReV8JzcH&g9^&_8tJX?`RzQ9WJqqOH$yFUwP&G013{ZPO&5=@770Oq71F5dc^`VtWxvKP{DwUyTgajU+l)Y-J'
    'ECkU|20{k{m1GW8q7R8Q)C&-PLwk{nDh3n8)X)S#A`VqVF{;4J5K=>-2z3(Fn$Qxcgdd`7X#Suuf%XyFM%95rhz)%L6dbLqYC*5D'
    'GGb6I6|_=qs>%lF><lE^8oSD?5>vGTLE~z)M)fAplO$iYi%=r9kbJAQ3rdX?sxB~S5Dw^rOswjtRH}MG-bktd>KkZapgqc_s;8o='
    '%%PglpdYO&cc4}Dkl{mjtD!=ysx6=b3=rl+2?V_XbWp{rlB+-~2b~QH9}1)5RNE@$FU1c9Fmxl(g+QU>HU5Z{KlHTFY(ZNL?Jycd'
    '3#ATJKhQ(9p(=5`#ww8-fXW3G(3Ywff_BwGE1;3RqAHzLhZ8hl5L8OgZ=nWKtjb_SD$3B~sO|;|sMJ=qZPf5WYP6B6Iy>8{4U$)q'
    'T7b$InlkA5vQxc{fu;j`Bvq<pRR=@;8=?MyzDG4JUUf`R)wa;XL2V4(Ayn7U1vw46BE=8Y8nk23>_MlDCgGqQfz}9`ENHHv0M;e}'
    'l?n9fP(ik_s+7<o*dy5^*&|<1kKE3g+chmaC_5-SD5p&JNcKqf$cLuP<@{<qU0(eKHYD;fi|*U@W6+}1b`o74OKvm=H1W6hRkOux'
    'GlQ4s%Y@K&85Z-oTJ#m;s+D{$YMba%$VS5lKV(-Qof?F`Pz3G$xggym+M{Q@(<s{ebEz#Msr@7)X)}^GBWW{|HX~^>k~Sl0zqUx)'
    'yS+JH2*GXrA>%y=cX;U!LAY%J;`L)o_;#!ZCv+opBXlElBXlElBXoOx(Cr!<tc|Z%lbeq4jqr`|jqr`|jqr`|jqr`|?e)XAFR=RG'
    'nNe@|5<5_N9;i4^wVy))*IV04%yBC*ZY9R8#JH6hw-V!4V%$oMTZ#SETZy6U)AM%`^U?;5Ef4G>CXG)pqJML{h;4y!?<=vx#d~q%'
    'BSOi)78nNtV*+CWV*+CWV*+CWV*+CW<EMeK;wEVqzPxovy!^JPV7OaMKF|B075zRlH|kZ?2T1F_Zy<H<cl0=l%I0)m8}-)72SME0'
    'Z4ftXSu<PDP>9m4cf0H8IXQI{)D73=vEwt3?7rW2Q;qNTLK~1^51{LAwHp423-`9cc0==SybXxAeFT8k;77O|Wu7RxRei++&-pf)'
    'Bi;xCnsk$^`oC|h*`{0fvGwX}{9j+5h*Pp!F20PlH5^}%np*uhIOUP&W-?oJSG|fsF3!8jy3>ZR>ZbF_Y#SxztK76y?Sy2sS}nKx'
    '8{N%i8FqEG(pW6JGY3nO8+pJ+n&e{IjVBsA<>N=cTa2L_dKscl|LWj0C{-MksvVRn4N8><r7D9`Su-q|9hS@uOE#?h?XU{4!)m||'
    's{%W$4(zZ>u)}J>4XXt=tQOp`T5!W^!40bgH>?)iuv&1#YQYVw1vjh~{IFW^!)n0~s|7!-7W}YU@WX1s532<~tQP!`TELXGyj#^l'
    '7_zb<S<`oyzPt3@r3E1^2x&n`3qo2D(t_~KSrGOj8ajW_`Gd|Mgl>dxgl>dxgl>dxuOGT?RcYgwmcqRi3|E{Xh9ia}h9ia}h9ia}'
    'h9icf11}wT>8C(H1^Ow_Pl0|4^i!ap0{s-|z)J^SI`9%06BrX16BrX16BrX16ByHh_q9Xf^+f(VUT=C)@$&k5-Kke?g}tNsY|%}m'
    '8E+kQMytuUgI`~6C%a#}+~&*4)k$WGrH?WTQrr8?^w%yk`StVX-s|_{f90kB1ML309cyjx@cMEtwc97`+?vhSt=SxVLEA$&W^)k#'
    'MqQe3Zs*<3rFOmbgROsWr#08x{yeI8Hr=~k&lY$6GvvhA$^5Qfy6J9~s~<Zx!N+~@#Qw@QDD?iq?W((;egEG(W*uDl;|3wpC}<Eu'
    'gAf{o&>(~cAv6e~K?n^(UVlIy?2odAZNC~?TW9(wlQ9KTFa=YbP0^2SW;>eg1~Ic8y&#&n?l#kZ4&?~djB77Ms4MKP5TXCo3la2}'
    'WHOmfb(g4pE@q#F>2EguZRY3C=2v75LB#<@W{$0>!SDZKruYR>gXUdw2)T_im9;UozO4eqhaPob=3swG<{&d?8?#K1wiiC^q(l%w'
    '9IcUYvI->XOcf*KKRd~UJ(Wxx-ki-|PO)G4-;-BVoU`Nmatnw<fyELf7u#BKlw449LCFOr7nEF3azV)jB^Pf>azWTe*hbh!*hbh!'
    '*hbh!*hbj)YGK<M(QP;t-TEWZtv?do`m>_jXIZ8Fnn<@r!M19~T;DA2{>q2~d;VujbTw<~OaXWn4%yPY0rtX9q<jBa_eHwyw}^D5'
    'BkV-F)mHCRq?=WMdmz$nqqOazB3)^KJCW|*$>zRHw=s2~Y3+G=?*8HnOLQOEp6E5L4YK0~;anSVZNRkw*9Kf0aBaY~0Y$oRK%`69'
    'M%YH!M%YH!M%YH!M%ea7MY`>&NH-pdbmNgoH$EiNeU5MN#Ufod&?7iL$0OKX;t?EAJ%a569>I<t!3^|6JWo7NJWo7NJWo7NJWo7N'
    'pJ1k73Z|e>@N4!7b|-$j(HMKiC%Ao%Pq2HyC)g*0J7t>lXMBR&Q=ee_fKRY{A)nxsQBlwI2~L`0FR9pxjLun)Z5@73ZN~s^fRZV+'
    '*q3R}6k1SdL7@eO78F`gXhER`g%<DIc8tJ{z>UC-z>UC-z>UC-z>UC-4#D?iu8%kbr{_2XyXQFsr&EVu`+!5Rdm)Elc`x&9hhY7v'
    'zs4c>RV@xsEnFOMalpj^7YAG%aB;xJ0T&09>JqpSxDmJ!xDmJ!xDmJ!xDmMh-9EujyDV&lv`?qpiC7&Sb?K-}kWG+HkWG+HkWG+H'
    'kWG+HM_s013Z|f=?kjWDeOVE}U*t7==o(A6S$bbkgg_AjMF<ojP=r7c0!0Wkup)3Ha3gRda3gRda3gRda3gS|+w2QVSLwq_R|q0?'
    'B6T8lB6T8lB6T8lB6W&b3ET+W2;2zV2;2zV2;2zV2;3Og7HwKSH(|DZBD8IOUTE8U`9B)nHlIefZHG9`S~|_rX_ly-sGg{vsGg{v'
    'sGg{vsGd%<Ou-aPL8sX_?ljvDahg380hl8ZfO$P40L~;#!4ypK`ezd2U#4IRrg+=<_fh2f8lkRvK-*;=@?ap;WeTQX3Z@{`WeTQX'
    '3PN2%T|!-^U<#(-q(P|56imSsgt~;fgt|<@6ih*=%TbFdn1U$?bqRF|b(w-Gn1WE3qZU&z1yc~}66$i&U<#&S3PN3`U<#&S3PN2%'
    'T}~QI!4ynEsLK>g!4w>|2z3c{nSv>pf++}fnSv>pf>4)Gmr$1}n1U%dX%Ol%1ye8up)R2=p)OM}1yc~}a@1l9reF#}T|!+#U8Z0P'
    'rXbYisKpda!4!W5)RnjBz6#339Rd^AcQ?S7wi#V@^V!#KHS5;;^Yij<aWz@}&_KVR=gaB8NLQNCx5;cXp8nHK|3$)VocBq-?4QZi'
    'vOei{HCwJ`n;-SZL~hiFeYpjybji9@lFhL>_^&mg7#Hw2=h8OThZKTIh*xi8P9`HZeKg5LXN)FGCLmcGTHlr&i#H{h;Le_m=T5fg'
    'PNs7w^SP7d%t>#~owQgn%x*RAbC&;J&nI7`=%NkH=*P_sCKj6-XBy`lc`M1;yT-@H=O)-DgeIgWnkM=twoNQeE2U!Plbrn3*N`s>'
    'UP?-y9_0gCDT5^CmE^rqwnfsFA`g(ftvte^vHH+RwFT42ouO?)X`*XlY+`O&+ej8|YLaP^Z&KSN{aASlYC&?9bQW*X%6YkLvpl`X'
    '(+fT$G>BjAY?dcUCCj%sU*w7SmW)TpopPhxC%4I6a+5_}%1u^ovRYe`xtGl4&8&R4Z6$MgA)6!}nnIRzMKU*%xsgw^@@XhNk3y7}'
    'kV11&UP2x#x8hSmBlpQ|1&vK|m%FUoW#ulb)h4;iUDDiyCHGd+q50v{v!pAMyR<xKBzLJe=OlM&7*6WR1$hf8Kq;*Tm!!Tw%58F&'
    '++?-6$_2S%<tD4GNivt4tlVU!PHijcq_Az0bXn3BNheLk8OhvA=2kutEyK%8NR>Hh5H5xWZJ@!mwvj6V)hc&cxy#~$T(R29C3m^Y'
    '%3W6OvQodcm2^qcWl5JMo%A_pBzG&hqcKW9a$Y`B+Mo;a7ScHIEe)<o%dy%xqqZ5;t(Egu`<~=2m!z-Cv(S4b?^fPH+K~(u!ji5?'
    '-bV5^lDD*DC+*ltJ9g5JowP=WcHE$qHMk<Zp+zG_4@hX3(zH{?GcRxV!t(yolASb0C#}(;IX1W`*HR-_9&V+0<mC-t9wU#JHjEbD'
    ';9At~UC`^~^rOuakG!<WHz;uoC2|2hT$+j1UL6~0&rh{WJn@pI-pB<xFQ+krw2ic6j2fwtW-P7v>E<X8yrhZiSQvDO%8fK&X}{8Z'
    'F=I&UwP>JEw?sU0Xxnb&7HPZEbfx7MOi+@oG~5dZU_5e2vu-50G+Sx4R$8r<hH0f`+Bz*=GX84+7}BbL=h%yBWoXm>og*)h)y?-Q'
    '5L7l&l%wxC$*h{?iSi0D`nX%h$(8Y{!}KeYZi9+P8<RSgnn9tTKsq!&G*{Fb-WlVwHb)GTCuvaz=z}_kCv*%{ve5?RPSRrbjc5V#'
    '@zUB*WJW8=p-mj8m8OUZI-v&9$&7Yrr+6Toqy<1o=<R5^M!ShapTv9~Ptr==NFPFLFxvQ>PWA!4O4`S9T4|XWIgD1NL*tSDEp1im'
    'NBYGH$c6B6wVX>4qwM7alaD$3B)JAtv{fO0DSB4~_Qj0RK+qrXarfm$u&Gv$6pOS$Bc-kB+T^4@gMca>i7{Ul!HE9&sw<^lHLvHq'
    '3XCD0RYC4g!R+(pw{A6_ca!UTa##|H^#176(Wr2ugCUVf?{ZMMyS<e`VYOUtI4F=vq}`xEq(CB(bWnf^+j=wE+^u_M-exkZ7G=}B'
    ')v8-;#-02O&1gN{%poYVxrE?zfd)~}xMsA0HqzSGr8c=7vfMnm-Q2CZF^tV@Fv;gAS5xw~-C3NST+9~YxqIen-F$UyuQt2VXMVjI'
    'FP00~tywRR4W`7{7Rkr?5ULH_Yz_!i?enVSPM%=fHbMT`oV(58eB*uVOewj^&;@wNs3QcvO>jouGd14E0GYjQLND(7z4^N5=Npi('
    'E79+=Eq%7W@em_FFWVN`wyG!4T#M~<jq<8l6=s8Sb;AoGUQ!6|ej!9-?3qGnFDV2!v=EXOf=scWCyRgg!a1u#RY<U1l~AivDX4@!'
    'CRKAlr`Ps6yPy)_w#wIiRkf+QOA5%VR9w|BkbI|J2^LgboO-29sLDXqB}G*j;qz>-HOak_4szqvOVndku0hGAVq{3qZLeZ1Rq>^2'
    'fZS_hVpaS>ePmV14AH*p^(3Wg<gDtBa<3q2dv%)X2OxTemeN&yqACNtYM!LUmR|kR_KH48=~a0Ry_2uX5!F*b3#U4x((8p%uR~L1'
    '7IZJF*6~$=sM7zaDkta&Os{oGy?QUIOug+jPNAwrR68NB<W*awnj0vDa@AR=hAj2koKUqj&_bzV9LiYRD_e5a=~>l3<zCAadqs$Y'
    '>L99yNVPRk0hOwtu&Ng;y$Yx8RWx382vJp6ssd?KRdGO%r&_1dYnD>4_Xw(QXsc$(SIv*=YJ=*mpwTtG+9vgCnW&nFwpUq&s!&l?'
    'o4k=%eUqwwpi|1d0uGum)d8qNC{%?H^s^qi3g~ccuc*mY5u+-s)N86@)m%e&tSSp=rEIT6DpjAXinZJ;e`2rP@v2@4s{B$7A=Kfe'
    '>H@85t#YrLYI`lUHVF1e_DJ@~SJNZ6bLMtI$_~m7$_~mYlRc6>l0EXFDRVi$8c&y3e}Msre9WTzw*45CliECDFo1jj;||*XrTJFt'
    'DX+al+uhcd5B=#m8Ec1*PP+~=Iex(98OnFh7PHL^{+rd+NQMpR4Bi(R=VX{|3%bnyT+X4Cf}wDCE}H0rE9k`gbJ8?(EEo<mY~i!o'
    'QikHP+P@ba{DOBWWaGnQ7V`mZbZ0TY&0qda$(#1@nQbwxbZ`tByK~8;5Q{;3-=C9N!$=2>hv$Oyt7wm&kzy)2Yroo`lNXjB7^U~;'
    '@H(NIY;MnGlcS8W;jrwojDoGv>0o~@$aLX#9NV9h7Y?mjE$+{ym~&Bc$^E(Fl6PvOxjR?nvr@F>@H>iYgFM`*<!Tjk`dC~!%u8xn'
    'J|5%A!MW6ykkm4=I*>uonAFzPt36>+ThORhw6;06$%kw!tBGw;D^7!94Gp%anV=3fumlWhKq?b53|k*}8)$WEfw5mu8(lRSeAMh8'
    '#(p|<!I(ExE9h1yY?ujVwFeAp8*4BzN{OiLs;l#t8p^`76lT7#71T*VnvJ&Qe(Y8wWK&09l!@92yE;~?QL;+;Q1+NCO%Dk$*{Q{9'
    'KYy!<tf><)h6c4@b#<Z)YA&vZw7w3`uAhN0yQz_IKU1hpvZ<pzdW{+lW2REWXy1bxOxT#3Lk~ePxvJ%Ag$vbM)>NZ+v^TY6RnaPD'
    'm)K9Dm>>WS+S1#*#@=GM&P>%*-stRtiNsb5L$xaoy{mw!roZT$x%39_v3DKl(TQ5knhGweLAl!RTAj_R!LX_Gk!gE-URS}K+OgM}'
    'u$s+dZdc=J(*r@Zb`QP7fT>o%Fn)EVw|<Ykk#!G-)TUcaW!2<A_IA9sn&kT)A^?yAdxkof13F3*g|yeIm2K(GetWzLEQMlyuxF`f'
    '7Uac6ZOLQvXs^;TzQ`Hpa%UR!x}%N@koah?La#gOxN3s?WQRhpJL<i?+5e!vq`mH5NA3;m2lXX&47t~vD|#N*m$q&m$Gy3?6(7`>'
    'wAbD1&AkK6puVIn;$CmAmi@!}lJ>fLy}7rbAJmt$*WK&Qz1PH`zNEeGUT>~WH&1ja?REFMv93rS>{#mXu<y8PLjPE|s>aig&W9)6'
    'YU@t9n~_7XD3ji~(zQ8wK%bqLr3}j{X^QG>Da}wyJGt`nVAqY8L2%yX2rL%EJqUMr=?_7;Z2`*b7iS3Hj@8_RZiH@xZiH@xZiH@x'
    'Zm$ozU1RIC@%3tQ(-FQAz7f6=z7f6=z7f6=z7f8?e)#qUM*TZO>)kG4dzI&bit|+aITUccwN1htHwoh=VcaB)n}l(bFm4jYO~SZI'
    '*k8R#nABH_0b--)?-UkfzKGax>+DWpXRp42ox-;0xOb`8g5v|3<0F#EzZM;H3tggPqGO_CqGO_CqGO_CqT~09j+fsS6&H7l$>(_='
    '#-iU==0^RHY%R*}`)2fEw*$yg{57ZB*r@kTItbU+Zo{=<hnm@XhN6^~yxUAi&tb2elkjbDe;S{8WcU5Hn`$Jt7um1`8~$8(tJUxa'
    'T)vA9wilXr<8AD_?Nb1{E_sTqSY+BLx%FDd=O6WLGDma~>4;M0lPeTHp#HbjY=iJ5GH~qP=KuO~wYa=od>L!2IKCh?w)$~=YK0Aw'
    '7Tr~^dXS6rZnEyQ6|B1Hd@|d{MfoZ>E&Dx;R;%T9SEIYREX=O1RvLU|cSaW}h_WY{Z=_i+rrmg=ky1W>^t-iL6p)re(haWB^|pU?'
    'tU8k_nzmNC{?$=xP+skzywae&@}RuRpuDUZl$o`|a&yCS8&>{ySOwT&HDHHTfgM%{c336YVYT3f)q)#V3vO5~xM8*6hSh=_Rts)e'
    'Ex2K|;D*(L8&(T`SS|Qrwcv-<f*)23epoH|VYT3g)q)>Z3w}r~VAfjRt?D2QG&ST6=+8@kUi$OWs*qNNv?`=kA*~8&Rruzt3VSgP'
    '9YW|3LWdASH$pc;H$pc;H$u1958bxvwDAi|;fUdg;fUdg;fUdg;fUdg;fUer)Jvycx-HOcfo=<QTcFzl-4^J!K(__DEzqf#PQ66O'
    'M8`zOM8`zOM8`zOM8`zOFNcoT6Z!9Wz3D~A%j@fP2T$7`0gvXhMK_Tay>;LjttQ__AGYp0f~F_CU%TAq%gNPgW`Na4nPsWqeP)75'
    '4>FTqKY#8$fj|CNUiv?P@h{+RYkMEq7j?AVK4j<cY_<;1=GZ^_)WLZdDMwwaZ*J$^&dGMYb&aimZ>Kfa+x|Q%c{c5OH|fP@wz%t`'
    'At%00=6ChdO?R_g{n#lFKJLRT_E)w6ruP?aSKamO`~Tiu>)^^CHxrR|K{FAWiO@`hW+F5bp_vHHL}(`R`a}0%%ako_`_<s%I@3Ry'
    'j47CcDVXAHihgV}+tF+{h?(u^1p&o%x0(KPC{3uwU0YGYcBQ=&CFpv4CrZFC$z(E}>c&y~Tn?lWroY+rx0#<mn_rYT1eFLBnLM@v'
    'htkUe1cza=_%(rpW?ym$xs5WRwK286t#ZbPo_1gIV1G&SAoFJ%vrLn=7eVYKMi4|Ct&~f<6-(6lD#|+_$S3TneB$utZ1&QM{rdl&'
    ')S{xD9p9H;K=KJhmMFj2){3M2g7OQ>FDSpD{DSfe$}cFtcvJEV!ZyM-!ZyM-!ZyM-!ZyM-!nRin+s+7Z!>Itb90_p!kpS1972rN;'
    'p;q^<-e#n~eqFHpNN9-AhlcnCxvu6eT`B<8!ev_;IUrxy3U<#t?!I8x{T9KlbcelQSFQI>1-n_rxCesWHcAU0D%h1~xD)Iq85T>z'
    '_;63M3&E`pH?2J{)!kowVY%-9C13RlAv<m%&J_Yz2wWj>g}@a8R|s4oP_X+31iOT7gl&Xvgl&Xvgl&Xvgl%tBu-l#pcHtv@AlQvZ'
    'g5CI_VE0jP;m{`M4+OjN>p6m5H{3@!KF3GcUFahmPkn^#13tozKEjX4Vh=9;`OV~WY>DcL>WS)!>WS)!>WS)!>ggp+zeuKF3Z{66'
    'Uc&Ca8*enmp79cHpW`L$9`+LU$>3(0?ED!o;r7%^*goJT>|V%ASYF0G+e=t(Zg=vEo%rZfY5@S*ANM5}Xf1$46kZ&ZYR(j1P<TP%'
    '1%($BUQl>J;RS^k@7t1$z>UC-z>UC-z>UC-z>UC-!0pXD37-pN@yl}EN1TMybDV_Tvz>&~sgtmMz)9G>kdv^ymwC36uzu85u6q!s'
    '@2r!s(+EuuZ2h>u__DIyM`8)St|bDhg-Zl35x7L)5`jwuE)lpy;1Yqd-8UfHC2%8fBXA>dBXA>dBXA>dqo44FQH2j>yH7hXY~{31'
    'r`(BH9o=>>myRA@qT4P(HbFK)HbFK)HbFK)HbFMscA0`Hn1XJ*zuj&3WkmpgkN516gY2Jko_#wa1o~i5gg_AjMF<ojP=r7c0!0Y#'
    '+`x*!jlhk-jlhk-jlhk-jlhk-?L9cpzN~bWKCJH=snZpLNS#QXNS#QXNS#QXNSz{90yhFT0yhFT0yhFT0yhFT0yp}wK1;;f(ub8k'
    'tOVHv*#y}H*#y}H*#y}H+4Ny$3Z`HR`mnwSAJ&#WtaOE-D+EOd6d_QAKoJ5(2oxbugg^r;0yhFT0yhFT0yhFT0yhFT0yp}w{)%*!'
    'KCE<wAW|n%CsHR;CsHR;CsHR;r-+rnjlhk-jlhk-jlhk-jlhk-jXtcGidfA9KCI>;5vw^8v6|Q8!%9~OreF%Dc>S&rbl_qNreKP<'
    '?ZEY@>(y)Y)gsj8q`?$S!4!nLOu-aP!BLA)mr$48gejPUDF}6$f+?7SP?u1bP?sr~f+;v@5b81oQ!oXgE}<@=E>kcCQxNKM)M5&z'
    'U<yKALR~^#reF%DAk^ik#S~1z6ok5jx|}qaf+?7SP?z<@6imSsgt~;foHUq%DVTy#mnoQnDL85o>JsX*n=l1aFa@D5Q!oWn5bFLu'
    's4H*LeHE06I|L@K?{50gxL4hL_O)Bhy0!lNyu4dnO;$fN>h9<Ha{8~POU>xpWVRVk|LLaxN=j#POa<`spUKs-KI(QgTdroCAN9vX'
    '?$oD!xdp5|2uS9Vb*Ur`ta1{zY;dfy(1zBxCCB1T2`0F+C*!%3?YWcb+{t|IWI1zE!%W>fX|ZIO-D>ENEdRZpPrgXeMH`yYkDD8f'
    '{&iVkp&FP+gXfeQ)C$3JG(1(Rfu=NoO|GG6G+<4sp=S^t#+l0l6V=c>8o(#lP&^tgr_{hV2#=#7Y*Gy^r6Fr_4L+mco)BaP-;(hN'
    'gc~c!R&GNWqf~<vX%wMc;{j>Zpi<-iAV!Zy@<}x&k4C}D8iS|C$tgAB4N6aA*Q6SZ2C-@o9m(jY1Pz^(5fQ1zE-E#q7-AJ^wWS)H'
    'NaF|P8VN|_1(h24r}#dGPpZ*)G-6J!k*qXAPLWQIJmj)G6#>#(L};pEl~N6mq+yhD4Tz*ci%JbYgdjy4NGR0+Um7?l*APG&D5%ts'
    'KM2dC!F!U1;fWeur=Zcum4Is1Xiu>Z-c)KBChz-zN~wlM(f~@ihC<SCMMVP-X?P+HA(Uz`APpImYw#b064V%4C|r%elM!eRZAOES'
    'Y3QfW2XV?Z9FqoZDmDBPLL_M*rKBN|Y7nwq!-;8_qEZ76AuJKvF=7Kn4f#_LgG)}M2P8C1#JbAc@K2Yu`#!)?=pz<o4IzdS(%zR4'
    'R_NhYs_~*OZum9IlC<HV!H)1q?cN2wPEJ4CJT<`7g-sqYm(Wx+fLX%ufrw%m5&jTG?1^@XCtlLjH6Byc0BHroleA=v8VOO#5L)cX'
    '=7?}m7c_Al3xf_(86nXSg|HyFpoT8f*k(_*M2%WCux%s$nzUVnRZ9qzRzs=fO9x;LdNrU~Bcvk2wv`%&4H4@!pjx_g{MG(3q*edU'
    'u@}?I(5C%6M_xdyo9|OVsBEMtN8fXjSvAWO<rQM|akq|>E8|s%=@%h4QSoSFQpZv=DD)FdhsNhML|N3>ff%2)IbxVRNsBTF8ltR4'
    'spA!kc0!8|f1DPxZ`9y|39Su9X0(zV+Qe~M#Pe*??xhCN$&7Yrr+6Toqy<1o=<R5^M!SjAD1rg~|2VDGjr1Y32BVG7>0}=?&|rI#'
    'R$3-T4x?4+G{j)g@SiRE#R<xV^l`PEOA$+b3D85tRrydKbM{HH4yI_+!b7C|T?xg+j3L3e0F>N)xe<h_bp!}hLM*YSYm>u&1zDvU'
    '7w+s;R|8@q|5tNf#m11)syO$jAouz5Telj|yUF!EX)K9EdVlokXjC}S#E?j&cR48B-QLQeuv#uR927_-(r!>7QXr8?Iw-)9ZM~Un'
    '?$*6ZZ!?)yyRzxsYSk?^<4%5tX0)De=8%@zTte`<K#!<rTr=80Cuwc#Qkz^3S#F-(Zthmy7$#>nnB;Smt0{Tg?(EJ^E@lfE-94kV'
    'ZoWFUTAN*sKKN$z^=7<SE?~W8y*xIU5@TB=ALm1;mT<E<AW*f^tKvI(f^FLbA!u{%Hjnd-_pLLf<R(KQ;324vkoq>k8F|mtcpC!*'
    '_qGYW%<uQ+>z<!)K)$X-zst7t*?LKx8~eO$TV&g+vOse!w$C-nt7g@j4a(IGFNAnWA-Ma65RI{C3ZcEE5Zus0NLmOo#eSYF{@n}b'
    'tZHAO1c1=L4OJu!5wwNsqx4GBwp1ZJG#D*Ju#k2_pq@e%m<LrdKo<dJ0+a`-RMB=Q5F0~v0O>!Z{i&_0kr<%=fYuHz6{7iURrKy5'
    'KZbS<N(qS2bE_h9RqR3mq4MjjIz=d0A|%bw7eJ5=@qW&#>9Ynp52)xMmxml4a(ActifAC~RZRpmmC#H=iKF^rs2!j+XiZfhLgkVm'
    'o`$#|;{7&O@jWzq4$^6;<)8pfrYbmtDk)m0$_OYQQmlGNG$hqStJ(l+g5;{kP+mx?0BRO!u%JmzscK5)jie6H44~G4iYdFQ(Tu8@'
    'fD!`=2q+wKQr)A493ILBi20$B%|Z2q5H3R!u8J$@O><Gbs8oQIKQtlG2tcg_g{HhuhF~5V3#ike)`jvBEy6=|51lU5U(min+lm&E'
    'A;gD11r^Y?RW0gOdnYvjEgdQ#*{Z{Y&eA}d53L$1pv_g|s2WDd`&9|1HQ=jyRbI$L1ya?0&{3sam7ziPc2GJ)KL_D^_EmQ&uOzhq'
    '<uG(&P@iQ}H9XK(q~7=d+N>N^shXg+fa)6B5~!w%Q4OdXE<nKywFy*k&>G3>6euO32u9&UUsjxIbEN#G_@V!Xz6<&-=(D`)|D^n('
    'rbh9%sp_84AlM_>BiSQgPmkQrncD>^J19FSJ1D12_DJ?f_Q;2(%;o%QJY8P>1qLAUF^lfo_G8c`)$kED1o;5^6UK;3^Q|USUMpS8'
    'b8FCtapyT1>%bSCwg_Z${D8|dl<%G`W}6wjIIF{v^aiP9?~C+b89v&Amc2ihb10>ti|@`w6Mb+c<->C_3g%eQ$ukV%vswm*0^=$C'
    '4Hz(kcPV7!<6~y?3GI4kHowhZ{!Pi7_VAf)F|CY*=nK1Z$)pgA!MLzLC!?W}xgZ{%3o-yjd-RMHQ^{F}@clV?Vflf+vp<K|3Ds_M'
    'doG(CW#|ZpWtXLMw?+rK{kb50*z1VDKPN97S~XqVpGz_4qBfKJbHyd^)KGJGuE=Mlh%xctoNI$T+^G3#6?6JnTsh23YFR!WQ`f<{'
    ')RvIcJhHlxLC~1g;MA*8VNrw7sHU{GIX29PY%8msZBSEAgBcVJwy3RORI^Ttkp)(y7+7Il_^9uI45Y2j&#?8Au}$p;AGQ65#a~nt'
    'P7CCqrqHk()BzSot5(eclUkH|Km~24t`5tlpYJVfePJ+I)WkJb<6v7SIkiMKb)vWZbfxCCvA0ZCDIdxnph8VZ`!QV2R&5<s)lSyb'
    'dByjDMeXb{F00MB>#+eQF*V)o=W(?ZwslHX!(>x|MeJuswYF93Xf;>u=OI8Cwc+pQ3^iZ1)c{ycW=*wxPi=3`s-jg)!)hknLlJ-|'
    'wdAd^p_&cb>ItCs@<wqCCR|%B4Ar<e^!@^M)>BhiwdAjJRP`UQ6~(FPtf_NmwJOiK_c92*)o_n|Fh#3TZx2$`nB7)?0JWWmwYgeQ'
    '1NE4G?p9Oq)LYp5YRIh?{dG>Pme#h~sH<VO+RLilf9#EUZMDl+OZFaU0TBR9#tMm~i9*_I)zr51w!g7A<F|?~AMRP|p9OhwQG@c>'
    'Jld<Yj4$%Yx!j`$z3!;v0!%;JtI+F?I<DH`KG~tr>yCPFZ~H%}FKMs4*O7ZG`$2t4y+iKx=DNxl)|a-v9>=}8Hy9t(m$cX2>&?9v'
    '%%HxcE#h8puIBy2`jYm#d%d|gqaW0lwAbD1&AscypuVKN?p|;1(fZ?EN_*YCZmdB2!H%U~5BrX*cJz;Rt7<&`=zMt6t+q~;yBRqI'
    'i!$k5EM1#}hsWT~*HVV%lr%+kxs+xorJY>)d9e4!%OJR&=VQDF;SMkTAqclEKzaS*4B^|cnw!v#(2dZI(2dZI(2daT^+C65Y@jy2'
    'UQKQ~!Z*S<!Z*S<!Z*S<!Z*S<!nfBC-@d@8e`jdD-74%r<$0juJk@><1zc}!r!dEz!njiycM9W9VcaQ<JB4wlFzyt_ox<MbPGMVg'
    '+`ClV+ebG(CYk(e(J}YYB|0WLCORfMCORfMCORfMey`|w`E5~gakrR!p7+5l`fX)y)DOwlqU^qJ<m7e-kfSJUPPeg9@11lIw5{C+'
    'ZIkO?XX_b?Qd;tEGaWsLy>d>1xWWBteCCne_uFo&@!Vcy!xC)xbKR|0!yj<@E;iU+Xx@#t5%9K80qDBqDY9abX`|%UYaO3|)VIkT'
    'Ax5MlN|jHpQ22oQ-&V7Y1}O>qxB0)mTrDmy7hlHODvmEmjjethpITvqq(yhts~+Uyyqm1iWFjt3=abnsJjz$OYuRsNv|25<+Zx@?'
    'Wo34CwbB?YyED2<L9ji^eIxC1G3~|^jhFKAqu(vgqF@ap)2){&BE!kC|Eq)5nN(4hL236_hp9n%wS)3XgYwFQ@+yP!vSv_b)(*?f'
    '4a;p<`P*R?V29O!9aaT)SRL45m0*X}f*V!~Zdfh2VYT3f)q)#V3vO5~xM8*6hSh=_Rts)eE%;%z;D^<MA65%~SS|Qrwcv-<f*)23'
    'epoH|A+>;MYk9Y-gD~LKkT;-1FCBX6&`YyInibNlkY<H6E2LTBn=>oy<uvpNp+^WkLI~Xm-3Z+X-3Z+X-CjR*+v?NCFD!*4h9ia}'
    'h9ia}h9ia}h9ia}hND+6y?W`lK)(h0Ezoa)ehc(lpx*-h7U;J?uU>lf5*-sA6CD#B6CD#B6CD#B6CJ-CI$lrYzvK0$7acFJuh$)1'
    'ZF>Yfn$H&9L|XLLfoHUud>eh(y6*^@p6q_@a+@zFSErewoR2cY$hgZ)rhvH548MN<+`9sQ{I9(9e*oiOz~9#PPOvZPX}f*M&g0o^'
    'J)X_6gY>C~^Db15`c~iE&bytL?Rx7PTmRlpYp%Ebc~tUj+Vy_Yi_L6t*FQr}e4Whi>ZP0RX1V&YQyzTW$64&JY$HtXFWj!W>)H4J'
    'y}Q=Il|ODKBJF}^A~X}BnF!59XeL545t@n6Oyu>)?!lHRTiEui(Z_YBe=-?UFa=XE#n}}7*k-n)*=`Us+tCXGitBDO{pV1cP>s8`'
    'qJ-^AdnZcJ_4ZDbfM1fyWIEN2qxQL&eHNy_+4Q%WpFf*llsE*H2*~PnCvYgeEI@D=CW~JaIB51IhmhMS6IvTn>)R@4eCTQSB@gzO'
    'Bo8uwwlT{zX?qdGPGST>#L-H*v|F)6ov)(2^MQQAp2{Z<Z_Z{ft=O;s?@28x+S&1a>4nbXn4T%W*w%`p{DSfe$}cFtp!|aJ3(7Aj'
    'zj#yf3&J+SHo`W-Ho`W-Ho`W-Ho~@73){{JaKotpw;Ty@{gD9IpB3OfX`xp4uHI&(zkXe?`$%kv(8q@O1-Y)~E?p`B)xu?48aW_e'
    '*a~*fJnp_=*Zmg3u5^dJU{|g8P6fMJ#kdE8-8M=KA1c_DX1EjV3VKc@p&#Fq?4lp&aMRlJQr-Q<7nbYpU-DJ25VGSI;#?tcg}@a8'
    'R|s4oaD~7X0tLHoK(I^LM%YH!M%YH!M%YH!M%ea71-tEuV7J&K!EQVf?8XNLyN`Mchqg#_+6#8&*K-8BZn%$de2$N>yU<5Cp85#e'
    '2YiGbeS{y8#U5Px^P9=%*b>zf)f3ed)f3ed)f3ed)zeFuevwSU6io3By@cI;H{NKBJ>w<ZKF3SgJ?tgylflh0+4(bG!tJS-uzkQw'
    '*u9XKu)K_WwwJKn-0tKRJMqz})B*sqKkiE|R6ql?OyR}8RCA{Ag2D?5FDSgA@Pfh%3NI+Uc;A*}1a1Uw1a1Uw1a1Uw1a1Uw1a5EM'
    'N%&kCi(i)OKH?;tp5r9!p6w)@PMw7915U#3g`9-tz09+ng!QAga@~V4eP^A7oknPSVC%>I#g~=sJ`zjtbuAH4EnFgSiNGZSmk3-U'
    'aEZVr0+$Gs?Y;roE`b|?8-W{v8-W{v8-W{v8~ubYj4FI6+kM)BVJoM7I^|Bp>gcw6xpegK65VzQvI(*YvI(*YvI(*YvI(;3w#yVu'
    '!4!1c{q1hMFDnB0d%S0l9Ay8T^X%IZA<zedA_R&MC_<nJfg%Ko5GX=;=LS{;ZUk-wZUk-wZUk-wZUk-wZtuZ)_GP83^kIG1NS&?_'
    'MCwH9MCwH9MCwH9MCuf=61Wk#5x5b!5x5b!5x5b!5xCKZ^;sg;mOiZXVI{~W$R@}p$R@}p$R@}p$fge~Q!oWn(1-Os_^`J0VWles'
    'T_Gq!pa_8?1d0$SLZAqNA_N*(5x5b!5x5b!5x5b!5x5b!5xCKZ^;e{;^kJnd1d%$CI*~e&I*~e&I*~e&Iz_AmZUk-wZUk-wZUk-w'
    'ZUk-wZuDWjRK#i?@L@F%iCE2%h}FCvA6B|TFa=XE#p`#4paU0EFa=Y*Z3nJLU9VoFuNI*$Ck>`x3Z@{`WeTQX3XWQYx`evyCQQK;'
    'OhKs26imSsgt~;fgt|<@6imTMgHV?#n1U$?bqRF|b(w-Gn1WE3qZU&z1yc~}66zA_G6hpG1)(lSEv8@!rXbWM)a9hX6imSsgu1LJ'
    'reF%DAk-z)<)pzBOu-a{x=g_oOu<o$P?u1b-GnKaf++}fnSv>pf>8JOL0x%^?yI0o+#xV=eRtD`#=Yw1v#;H1)~)sD=jGkvYO?yF'
    'QFlMjm(zdybl)bk&3O7xH~kk0TWeF6v+I8*SIhdO+tqBjnr(j69}~GzANJ)|<Js=xk-21DDoNv?oJ1`f9I7m|q4jOav3MhQ26y&k'
    'Ja@7^cQTzjna`apXHIIAse30aRt&RSjU1BYzt{807b&`ELo@nubECn(E{iKv<ML?ioKj<2Ay$q?r%E->l*X^gH4=@+t0^_|45Gt0'
    'b9r2%8ktAq_v9LhN2BGG8utd#aWsNWs*$BMVok2GXEfRqV$9%MG9H0wV+GmDZHQu&YHT76B9v=5APpK+YWN?7=+QtvsfOgyAXr&L'
    '@YFClr3Sn~>1pViRAbQ~R1Jb78U2)?k&`k4BGu4ErG^wks3NVlR6`SK_@G<^0cp6PQUm`K--qx?H5iWu%*i#7l?KQu(#Mg9T$ZOI'
    'K3a<aO*N`gs_~IDic+rekThmdsnLfJqe$Zjr5fK$;|Ap#0Z8Knl^XE}QF%0WPtqtnQG@FgG#a@QP^}v5DfY3ON{zzgeIHLL)yPO1'
    'KPlHpNE)rEXxt%<PNWfpQjG<q5rcA#{UeRTYX~hAt_I-Ah%<*aqp`;{@>A$zIOQ6RNn<vZ8hr^7k~EG|(uhbk23fAr#577#squyo'
    'l?d$^p@E`C{3!^*C8yB?5*j8#UFB``r_0)XAKxhS0gJLm5JL%R?@Nd(^l&THa8VaG{2F9Q+HlZVM|h-m?}A<@ryp&e8ei(dCXbLy'
    'Xet`NEMfRSK(UMfe+VM>M7zWjFKOx;jwxz<w1VMDS~5nBgdk;zEcRq`L^P-inz)XIL5HY}h-e5xSP)xKBbRAtvnN}k2CW*{wh?|!'
    '+AgB1CB#Xqk<{{~1F*)t8qll}Q4vwwN{zyXfOQ&AEnPbPYX2D0s(<I$i)m$O)Bc?!FOb#E_bCumHd2(M?>Wh=n&pY|3NiY)TgJ(i'
    '@v6i0i-?=3c(gI8W2qSw`U#{%<MSG!ENbXLjL+H}F-)GMMHvK*P}ZW<@rp$|p+$#3PK(($YHY!T)`lW8T1gIV;y5kBdA4ZxQiJGZ'
    'Mmw}qJP=OO0w5&xcC=ih-Nb1S!GQjMoL1^a`Vd-!(Z=U=vJV<(usumDEfXV$(W-PBVK8X)&ldgS1mr^axLVGoh^4+#3<$U?AL?Vy'
    'K1tTW6m42~h?Ku8p_rI4Bp4R}lDjWA0#LP%0D($~CDwFpa`>+xt5n0noxSR6JWS;OYR;?F7$RDg=Kd7mK3{(8R^xd$xxOciC6P$)'
    'k3JoZ3MYyf5{dLK2Zg)aTNxBq%jJfH0*OT04GKgGBoavn1t|B{%e&RI+lv%bAV?;Y$$ywt)(_<q{#sQtI2H_<OeT}bWHOmdCX>lz'
    'GMP*!lgVT<nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmd'
    'CX>lzGMP*!lgVT<`G3Wqn$dcBx0-h2$@QjNjXr#2p^(Ys*TB}N#unfIl|Q9%EEqDGOeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_'
    'OeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_OeT}bWHOmdCX>lzGMP*!lgVWA|B62~'
    'qpQhgGXC6MFIU~@!$;f3hD;`t$z<}MV<Ti!mj7Fp2buhyn0=p2ewXan_sQgU$)0_mOeT}bWHOmdCX>lzGMP*!lgVT<nM@{=$z(E_'
    'OeT}bWHOmdCO^Q%@ZiCN2ai{MfSh%WEgZCYyuZV;{quOohhzKa@s1DA_RoU{4<0;t@ZiCN2M-=Rc<|uCg9i^DJb3Wn!Gi}69z1yP'
    ';Bn^I#{ZjKZ@Lx3|B=aL@~a8|=0jr>OM`*`$nS-VYf$TG0xS;3{3E}cm~Wiy8SRhd@!q1nvDY)sAIpPGCX>lzGMP*!lgVT<nM@{='
    '$z(E_OeT}bWHOmdCX>lzGMP*!GtM6m9y~6IvsKg~3I3-n42JpR!GmG`-q-N&IQ%op-}`O;p3R>@{&?`<!Gi}69z1yP;K73j4<0;t'
    '@ZiCN2M-=Rc<|uCg9i^DJYHFlzs=<Hyjzbych}2RH~R3g8k(9WHQqIbL?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF>r<L23vPGE0I)'
    'B9Ta>zkJ3wobX8`5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{li'
    'NF)-8MEcG2sTpl1pXc3rJh|R<tI>y#+e^M67yyfiL?V$$Boc{4B9TZW5{X12kw_#Gi9{liNTk1mtnn}fV3=SMi9{li-oc!2Th93;'
    '5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?V$$Boc{4B9TZW5{X12kw_#Gi9{liNF)-8L?ZoW`t<Yv'
    '2fU7za{'
)


def produced_terminal():
    import base64
    import json
    import zlib
    return json.loads(zlib.decompress(base64.b85decode(_FOCUS_TERMINAL)))


def synthetic_ds_terminal():
    # A deliberate synthetic variation of the retained CS report. Change only
    # the declared terminal fixture and its precise faulting prefix; this does
    # not claim that the DS variation has executed on the native runtime.
    record = produced_terminal()
    record['profile'] = record['address']['profile'] = 4
    for cell in record['owners']:
        owner = cell['owner']
        for key in ('context', 'code_context'):
            owner[key] = (owner[key] & ~255) | 5
        packets = cell['compiler_requests'] + [owner[k] for k in ('expected', 'deferred', 'stale_reply')]
        packets += [v[k] for v in cell['visits'] for k in ('request', 'reply')]
        for packet in packets:
            if packet[0]:
                packet[6] = (packet[6] & ~255) | 5
    actor = record['owners'][1]
    owner = actor['owner']
    owner['fault_address'] = 0xFFFFFFFE
    for frame in (actor['entered'], actor['final_frame'], owner['current'], owner['terminal_initial'],
                  actor['visits'][owner['visits'] - 2]['prefix']):
        frame['x'][6] = 0xFFFFFFFE
    for guest in (actor['guest_after'], owner['guest_after']):
        guest['gpr'][3] = 0xFFFFFFFE
        guest['segments'][0] = [8, 0x300123, 0xFFFFFFFF, 0xC09B]
    actor['visits'][owner['visits'] - 1]['fetch'] = dict(offset=0x2002, linear=0x302125, raw=0x302125, gpa=0x302125)
    trace = record['coordinator']['trace']
    at = next(i for i, word in enumerate(trace) if word == (91 << 8) | 1)
    trace.insert(at, (60 << 8) | 1)
    record['coordinator']['trace_length'] += 1
    return record


class ProducedReplayTests(unittest.TestCase):
    def test_strict_requested_identity_and_complete_schemas(self):
        self.assertEqual(oracle.check(produced_terminal(), expected_vcpus=4, expected_profile=5, expected_actor=1)['instructions'], 49)
        for kwargs in ({'expected_vcpus': 3}, {'expected_profile': 4}, {'expected_actor': 0},
                       {'expected_actor': True}, {'expected_profile': 5.0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                oracle.check(produced_terminal(), **kwargs)
        changes = [
            lambda r: r.__setitem__('unknown_evidence', 1),
            lambda r: r['owners'][0].__setitem__('unknown_evidence', 1),
            lambda r: r['address'].__setitem__('unknown_evidence', 1),
            lambda r: r['owners'][0]['owner'].__setitem__('unknown_evidence', 1),
            lambda r: r['owners'][0]['visits'][20].__setitem__('prefix', {}),
            lambda r: r['owners'][0]['visits'][1].__setitem__('service', True),
            lambda r: r['owners'][0]['visits'][0]['request'].__setitem__(6, r['owners'][0]['visits'][0]['request'][6] ^ (1 << 25)),
            lambda r: r['owners'][1]['visits'][9]['reply'].__setitem__(6, r['owners'][1]['visits'][9]['reply'][6] ^ (1 << 24)),
        ]
        for index, change in enumerate(changes):
            record = produced_terminal()
            change(record)
            with self.subTest(index=index), self.assertRaises(ValueError):
                oracle.check(record)

    def test_actual_terminal_cs_and_synthetic_ds_reports(self):
        for record in (produced_terminal(), synthetic_ds_terminal()):
            summary = oracle.audit_development(record)
            self.assertEqual(summary['visits'], 50)
            self.assertEqual(summary['instructions'], 49)
            self.assertEqual(summary['native_faults'], 32)
            self.assertEqual(summary['compilations'], 29)
            self.assertEqual(summary['walks'], 177)
            self.assertEqual(summary['trace_events'], 503 if record['profile'] == 5 else 504)

    def test_terminal_fault_preserves_prefix_and_drains_real_peers(self):
        for make in (produced_terminal, synthetic_ds_terminal):
            def actor(r):
                return r['owners'][1]
            changes = [
                lambda r: actor(r)['guest_after']['control'].__setitem__(1, 0xFFFFFFFE),
                lambda r: actor(r)['guest_after'].__setitem__('rip', 0x2000),
                lambda r: actor(r)['guest_after']['gpr'].__setitem__(1, 0x55660001),
                lambda r: actor(r)['owner'].__setitem__('vector', 14),
                lambda r: actor(r)['owner'].__setitem__('entry_rejected', 1),
                lambda r: actor(r)['owner'].__setitem__('fault_address', 1),
                lambda r: actor(r)['new_code'].__setitem__(32, 0x1F),
                lambda r: actor(r)['visits'][actor(r)['owner']['visits'] - 1].__setitem__('entered', 1),
                lambda r: r['address']['terminal_data_before'].__setitem__(0, 0),
                lambda r: r['address']['terminal_tables_before'].__setitem__(4, 0),
                lambda r: r['owners'][0]['owner']['saved_store'].__setitem__('gpa', 0),
            ]
            for index, change in enumerate(changes):
                record = make()
                change(record)
                with self.subTest(profile=record['profile'], index=index), self.assertRaises(ValueError):
                    oracle.audit_development(record)
            for moving, target in ((91, 92), (93, 91), (94, 95)):
                record = make()
                words = record['coordinator']['trace']
                word = next(word for word in words if word >> 8 == moving)
                words.remove(word)
                at = next(i for i, item in enumerate(words) if item >> 8 == target)
                words.insert(at, word)
                with self.subTest(moving=moving, target=target), self.assertRaises(ValueError):
                    oracle.audit_development(record)

    def test_actual_address16_report_and_held_routes(self):
        result = oracle.audit_development(produced_address16())
        self.assertEqual(result, dict(owners=4, visits=39, instructions=39, native_faults=26,
                                     services=26, compilations=28, walks=162, table_reads=324,
                                     table_writes=46, maintenance_heads=36, ordinary_programs=5,
                                     events=4, trace_events=453))
        for cpu in range(4):
            frame, _, guest = oracle.seed(oracle.configuration(3, 4, 2, cpu))
            self.assertEqual(frame['x'][11], 0x100080)
            self.assertEqual(guest['gpr'][7], 0x100080)

    def test_address16_truncation_and_paged_route_corruptions(self):
        def visit(r, cpu=0):
            return r['owners'][cpu]['visits'][1]
        changes = [
            lambda r: visit(r)['fault']['x'].__setitem__(20, 0x68AD0020),
            lambda r: visit(r)['access'].__setitem__('offset', 0x68AD0020),
            lambda r: visit(r)['access'].__setitem__('linear', visit(r)['access']['offset']),
            lambda r: visit(r)['access'].__setitem__('raw', visit(r)['access']['raw'] ^ 4096),
            lambda r: visit(r)['prefix']['x'].__setitem__(6, 0xFFF0),
            lambda r: r['owners'][0]['visits'][2]['access'].__setitem__('gpa', 0x33440000),
            lambda r: r['owners'][1]['initial']['x'].__setitem__(11, 0x100180),
            lambda r: r['owners'][2]['owner'].__setitem__('walks', 1),
            lambda r: r['owners'][0]['owner']['source_ack'].__setitem__(4, 0x200143),
            lambda r: r['owners'][0]['owner']['maintenance_acks'][0].__setitem__(4, 0x201143),
            lambda r: r['tables_after'].__setitem__(8 + 4096, r['tables_after'][8 + 4096] ^ 64),
        ]
        for index, change in enumerate(changes):
            record = produced_address16()
            change(record)
            with self.subTest(index=index), self.assertRaises(ValueError):
                oracle.audit_development(record)
        record = produced_address16()
        words = record['coordinator']['trace']
        change = next(word for word in words if word >> 8 == 80)
        words.remove(change)
        last_complete = max(i for i, word in enumerate(words) if word >> 8 == 5)
        words.insert(last_complete, change)
        with self.assertRaises(ValueError):
            oracle.audit_development(record)

    def test_actual_subpage_report(self):
        result = oracle.audit_development(produced_subpage())
        self.assertEqual(result, dict(owners=1, visits=12, instructions=12, native_faults=8,
                                     services=8, compilations=7, walks=0, table_reads=0,
                                     table_writes=0, maintenance_heads=9, ordinary_programs=2,
                                     events=1, trace_events=97))

    def test_actual_report_field_corruptions(self):
        def visit(r):
            return r['owners'][0]['visits'][1]
        changes = [
            lambda r: r['data_after'].__setitem__(4095, r['data_after'][4095] ^ 1),
            lambda r: r['source_before'].__setitem__(4096 + 0x123, 0x89),
            lambda r: r['source_after'].__setitem__(4096 + 0x123, 0x89),
            lambda r: r['tables_after'].__setitem__(4, r['tables_after'][4] ^ 32),
            lambda r: r['owners'][0]['owner'].__setitem__('walks', 1),
            lambda r: r['owners'][0]['guest_after']['control'].__setitem__(1, 0),
            lambda r: r['owners'][0]['guest_after']['gpr'].__setitem__(8, 0),
            lambda r: visit(r)['access'].__setitem__('gpa', visit(r)['access']['gpa'] ^ 4096),
            lambda r: visit(r).__setitem__('far', visit(r)['access']['linear']),
            lambda r: visit(r).__setitem__('continuation', visit(r)['pc']),
            lambda r: visit(r).__setitem__('memory_before', [0] * 4),
            lambda r: visit(r)['fault']['x'].__setitem__(4, 42),
            lambda r: visit(r)['prefix']['x'].__setitem__(6, 0),
            lambda r: visit(r)['request'].__setitem__(11, visit(r)['request'][7]),
            lambda r: visit(r)['reply'].__setitem__(5, 2),
            lambda r: r['owners'][0]['owner']['stale_reply'].__setitem__(5, 2),
            lambda r: r['owners'][0]['owner']['maintenance_acks'][0].__setitem__(4, 0),
            lambda r: r['owners'][0]['owner'].__setitem__('retained_before_release', 0),
            lambda r: r['address']['code_before'][0].__setitem__(0, 0),
            lambda r: r['owners'][0]['old_code'].__setitem__(0, 1),
            lambda r: r['owners'][0]['event']['packet'].__setitem__('rip', 0x2000),
            lambda r: r['maintenance']['request_history'][0].__setitem__('serial', 2),
            lambda r: r['owners'][0]['visits'][20].__setitem__('entered', 1),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                record = produced_subpage()
                change(record)
                with self.assertRaises((ValueError, KeyError)):
                    oracle.audit_development(record)
        for bit in (7, 8, 10):
            record = produced_subpage()
            visit(record)['esr'] ^= 1 << bit
            with self.subTest(invalid_syndrome_bit=bit), self.assertRaises(ValueError):
                oracle.audit_development(record)

    def test_actual_trace_orders_cannot_be_fabricated(self):
        # Event positions are selected by meaning, not fixture-specific indices.
        for moving, target in ((80, 5), (84, 83), (73, 4), (20, 25), (29, 27)):
            record = produced_subpage()
            words = record['coordinator']['trace']
            item = next(word for word in words if word >> 8 == moving)
            words.remove(item)
            at = next(i for i, word in enumerate(words) if word >> 8 == target)
            words.insert(at + int(moving == 20), item)
            with self.subTest(moving=moving, target=target), self.assertRaises(ValueError):
                oracle.audit_development(record)


if __name__ == "__main__":
    unittest.main()
