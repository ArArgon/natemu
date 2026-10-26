import copy
import unittest

import check_i4v_ordering as oracle
import i4v_ordering_model as model


def report_fixture(profile=0, n=2, rotation=0, seed=37):
    """Synthetic declared inputs/effects, not a claim of native execution."""
    c = oracle.configuration(profile, n, rotation, seed)
    vm, capture = 0x12345678, oracle.PRIVATE + 0xA0080
    record = dict(case='ordering', n=n, configured_vcpus=n, tested_vcpus=n, profile=profile,
                  rotation=rotation, seed=seed, trial_count=32, vm_nonce=vm, owners=[], images=[],
                  source_before=oracle.source_image(c), source_after=oracle.source_image(c),
                  tables_before=oracle.initial_tables(), tables_after=oracle.initial_tables(),
                  common=dict(compiled=(1 << n) - 1, epoch=32, ready=[32] * n + [0] * (4 - n),
                              done=[32] * n + [0] * (4 - n), stopped=(1 << n) - 1))
    outcomes = sorted(model.allowed_outcomes(oracle.NAMES[profile])) if profile < 7 else []
    frames_by_trial = []
    for trial in range(32):
        observations = [[] for _ in range(n)]
        frame_by_cpu = {}
        if profile < 7:
            result = outcomes[trial % len(outcomes)]
            owner = lambda role: (role + rotation) % n
            if profile == 1:
                observations[owner(1)] = list(result)
            elif profile == 3:
                observations[owner(1)] = [result[0]]
                observations[owner(2)] = list(result[1:])
            elif profile == 4:
                observations[owner(2)] = list(result[:2])
                observations[owner(3)] = list(result[2:])
            else:
                observations[owner(0)] = [result[0]]
                observations[owner(1)] = [result[1]]
            image = oracle.expected_image(c, trial)
        elif profile < 10:
            # Vary the permitted linearization order each trial.
            order = [(trial + i) % n for i in range(n)]
            returned, final = {}, 0
            for cpu in order:
                returned[cpu] = final
                final = oracle.token(c, cpu, trial)
                observations[cpu] = [returned[cpu]]
            image = oracle.expected_image(c, trial, returned, final)
        else:
            image = oracle.expected_image(c, trial)
        record['images'].append(bytes(image).hex())
        for cpu in range(n):
            initial = oracle.trial_frame(oracle.owner_initial(c, cpu), oracle.token(c, cpu, trial), capture)
            frame_by_cpu[cpu] = oracle.expected_native(initial, oracle.variant(c, cpu), observations[cpu])
        frames_by_trial.append(frame_by_cpu)
    for cpu in range(n):
        v = oracle.variant(c, cpu)
        code, length = oracle.code_image(v)
        initial = oracle.owner_initial(c, cpu)
        request, reply = oracle.packet(vm, c, cpu), oracle.reply(vm, c, cpu)
        bank = [((i * 37 + 19) & 255) ^ cpu for i in range(512)]
        cell = dict(id=cpu, variant=v, trial=32, completed=32, expected=request, request=request, reply=reply,
                    walks=3, table_reads=12, table_writes=0,
                    routes=[0x100000, 0x101000, 0x102000 + cpu * 256, oracle.pointers(c, cpu)[2]],
                    code=code, code_length=length, isar0=2 << 20, status=1, detail=0, current_el=4,
                    sctlr=0x1005, actlr=2, tcr=oracle.TCR, capture_pc=capture, service_pc=capture + 4,
                    initial=initial, current=frames_by_trial[-1][cpu], captured=frames_by_trial[-1][cpu],
                    bank_before=bank, bank_after=bank, published_code=code,
                    final_native_leaf=oracle.native_leaf(), compiler_requests=[request], trials=[])
        for trial in range(32):
            value = oracle.token(c, cpu, trial)
            entered = oracle.trial_frame(initial, value, capture)
            final = frames_by_trial[trial][cpu]
            which = profile - 7 if profile in (8, 9) and cpu == rotation else 0
            faults = [oracle.zero_fault(), oracle.zero_fault()]
            if which:
                faults[0] = dict(frame=entered if which == 1 else final,
                                 esr=(0x25 << 26) | 64 | (15 if which == 1 else 7),
                                 far=entered['x'][6 if which == 1 else 5],
                                 pc=oracle.CODE + (4 if which == 1 else 16), spsr=0xA00003C4,
                                 resume=oracle.CODE + (4 if which == 1 else 16))
            cell['trials'].append(dict(epoch=trial + 1, token=value, completed=1, faults=int(bool(which)),
                                       captured=final, fault=faults, actlr=2, fence_completed=int(profile >= 10)))
        record['owners'].append(cell)
    return record


# Actual native output, retained as test data rather than semantic expectations.
# i4v-ordering-dev-20261026-02/ordering-xchg-post-n4-rotation2-seed37
# Original command.stdout SHA256: 23d287b1e3a1626cf39aaf51f8cf088b4eb0ba0e0c963185c351b934d141daff
# Its captured checker was intentionally closed; this fixture does not relabel
# that retained diagnostic as an accepted campaign.
_NATIVE_POST = (
    'c-rmV?Q+}LmA2t?;cxyRzJWd!mrF%SRJW=X*`Z{2@}_d`?^+vlH(^h#o+R>+;x+eRrcFJT1rU2<t$T~WKUO!}r`_tO)rbA<Zh!yy'
    '^J=|%T>aFpS2rIX@9uy8wBOxceZKkl>8ZT<ynA|Hyzp`V;qLw+zQMF!?LRzkpYK1!U$57zr`_&$^;0v%-`U@9AFggbe0qG27vf8w'
    '|8n*C;qfN^Qa|)lQ`N&Xc6HPB?bvMAs}H|D?)LHB-~D5CAKxNg`Miy9k$?NkcbmV@&4<5yeAqpge<%L9``gFeP5INis;<YX?}xEl'
    'SL^@jAKAp~K{rlSGu8E`8`ssQi{!ZO+jU!aO*8aE#M*ULM{=_s-p3B^{`x5nz-#^kA0PgH{O$XH>)Tgd(@s@8)I-<T{n&2mezUIY'
    'sjkMp9{TnC_ve4|x9!8Paj=^>vfJy2-PL}#y^TM|4|RCqxBY#*>*amefB01X^If%R$1c86{F`+@)W84R@%pqG$M^Z(-0m_s#n0cY'
    'n{{0^>uQR>QIGMHL~@KD^Z1KRwT?IO5R84OyZE-<w62=?OH+Pj>f(!S`QiVI9DdfThu!1P&%eZ}8z=Ss({{g!V-SZi{>5&J!#ma8'
    '*!6K*$0?qtezjiR?w+^z59PqzeA@4KkIz@ThcYOhZk`|ZtDnYdoZ7Y-V`yyWSMs3V#Q(3Vd>@8-Y{&S^+mFvN%&tD(#24Dl(8NDf'
    'kA2sSn;b^_&-XXqd~u35Gk(s;`{yzm{;~R79_%K*@6a^))>nPkPw}oFy*7^RG)?_<^jf#cU7UvT__aDtlWrP^b=}tSHdjaQ^8BtJ'
    'f7gD~x0`AcZ{5*rbyK(Prmeo559MzToAx*}G{ewr`s07OpEiAb!(ljjZP*O?2IN5a<F&eNHuWaXhojfpD&DDT@ox-WjG3{@6Y7uG'
    'h8RCpjIZO@^2cf8&quF~o7nRqj`;CwF>3ow)gB+gv5nE!RNe7@#L?Tdu_wp-F%I1rgLOFGk2tA@CJ*+}Yg6pj&{fm%eoS@W#y`G^'
    '{rGYQG+mql$KNa7>)82t>yBTGi6bU~CeQJ|#`*ECirFT9sNAO*V==5^D8@ir$9T-)*REr_jy-Ohb=$vRzpG-XR&f@r>tP*pP7{N)'
    'UpG_CUhB47$Eh3VM%S*pVI3!0-^AZ&x^*+I<Be})jvLn9X5BUG7&bAE`+D7X>v+G1YCXh{F|5b<x!%Pn@49syzP`$tD~{O^^HQ^p'
    '12s(Rv0lf~i4!PhiZ~=wOjz&Y1delY=+<%8j@3HmkvLVS&3cL%qF+z(=4{G@6kEJ0RUDA2jlULWPz=NvHF4TZc}AAAGqyM&c|`J%'
    '<S~hZQqGPzEIC)j(aa-}$9|LZU3@j?>ngq)ho{QpUFQ^@?{yy6W}dL(C{=No;*^YS%<oXgSL^s{opWHGL3sw|J0IUAM@k&A7`8Fx'
    'ZF2gKt)HjAIBqeP^E<?wQ_jaW&x81Co`+?U?AGP{izAp5XF2cUAXae{%lQ_^G0vX+4qaXv;;T6?^mz)zS93ndu^k7r%6T9TX_=ej'
    'pvK7<+hN|Gru+`^=EYEpExsDlQ#Hm{b8N<;%}Kh-F&Rg<j-y-0(XHd?)^T)mQm?D|oEpo#U(WC;zPgF8#?h_g=*EzaRGg7j&h&Ym'
    'i0_i8UmV>yi(|`kEG~AD%u}-}*Mqu>uhw~ei|-OgH?DJ$jPDXhw~nJ*w|NdnE{<;9<vCa9S)ZqUUW?+yuj9<Go1E|ByTsA0<LK6L'
    'bn_Ba$I*>bbe*SC{?&M6V_J+YUXBq^#}KIVu>Q-D6@Mv)&N|M_a-omo^Sx13j`2G8CVq_gDW-B!{NAX_QMVYl>-fpPJF0TjEe7s7'
    '&V%ocsvLETfxC|P{pA6bqi!*J<BRcyKR1@jsQYFh#};4xb7LvL!?&Y3wwzo4)L1H`?wipZi7dVT)L1H`?wipZTYUA;jiqu%d^4KY'
    '<*M=L#!?w|-;Cx+#8>~^SSq9Lo6#Iwp5}jQER|9B&1jA-zWTzDDx>b3VH{ht{+9zQ|IX3CUFV7Oy<wGy=!NUySHtRkTvMMu9@S)R'
    'TjeoXJ^sI&&r#?_)!lS$7iY)n?qezW`e7JK;_l;9{yA&QcxC$M@8#ob)p46suHe2Lzj}&>Xrp>tHUKI&m7B^<<)(5|xvAVvuyUK-'
    '-L2A1>85m3x+&e1Zb~<$o6_y%OSkLo<F8lOySoqjd7(N7cfM13Md^omCe7WibI6xVep9CS@`mIw>GJYA9agA^W`39QK#h6PE#oN9'
    ';h~@3yEuHYe`S%po6qcFn%~hlsIl95X6Bg7^L?z#`@G3xk!Nc;nqxWt^1W~J0Oc8+hd9T@IF|FF&!d)Sb&l6=USQ92WXeOCXL^p|'
    '96wWA&YO4(;{fOBp94DwRi4l}RJ%OvIVN(n_sx8k=lC7-&B%e0Q$XL(r+%JxRlY@KNwzLyvW$mvF6Mlgb5%|jLp=|YJVA%@s|q=T'
    '<rFe>^U#SODBgj*oaVfi6U#8p7pM4%;w{KwnX_F^H)Azlvf@XI_aFyr&VhMBAKUptmUDa#;JiZQMLt)x<0tBJ;?F^yCtyw$)20lO'
    'F>9<G=sAVu^f5JM*u?RV!=IzSKKxd~)R(aoZ$KRXoDy=f%ZVljc{w3+vd?)UXTZLjM}CgFO<n@#GP6ucWje^A*yM#F=j(g}hH9Q8'
    'a(woAxhS)Ez5_!$&m1{wr@V?(bA4Lov@)M`<yR?kUd$OkXWMZz&oc2N#k)|tiD{mL$EM6ZRgTJ>L332+ygc@0K5BEQ=B%1SJ?H9i'
    'Ds$A3m&L4F@*0x!_f(g8D~^90{w!tcYM#{dw>HY9A}^siBj(j6XZoyI@|5WF!kW`$UV`!$KeCL;Gs1{8B8|w?8IkV~*UatR^KS2w'
    'X@DA_E}2H85ottz=aTvH)Ahss(=WT*t8@C5=)3#J?ZegM_WAyE{%Z90E`IRM8kbBxY+?$V#<3Z@p&oP2JbtAgr^!*eT;9(g$hm*m'
    '?XM0WlzA%aU$5~|nfHee0?i*2+I{?R^Go?m#`A|?clr6F!zUMxKOz)=y4!wwc%DDX^2Y}<?sktiyQ`z%#<H$uUCX+bbuH^!*0ro_'
    'S=X}e87=FU8gH%&^Go@6w)6kbkI?2PYCj%c-|wD2{be^t_S^igx!C(ptKd=|ycm;^kdXKv6Nd?L^JNPBedgPInesOO{O8&fCm|v6'
    'cBi=aWs;liUcIz?(TA~n>>j(v?y-C99=pfxv3u;E)>lG8g55L4Nk~YXhuv$Ib}uS?c8}d-_t-slkKJSU*gbZS-P8I?NJy}IrZ@=+'
    'iSw{~?b7Z=)y?j)d+Z*&$L_Iv>>j(v?y-AXUkM2bcFz<iAt7-dcCTC5y(x-Rc8}d-_t-slkKJSU*gbZS-P8I?NJy}IrZ@=+iSw{~'
    '{nG9=o~ps_v3u+uyT|Uad+Z*&$L?u;B_t%+JyV>7gv7bny<uthT2Iwr_t-slkKJSU*gbZS-DCH(z7i4=?4Bu3LPFwP?B2Mvd%dS>'
    'uzTztyT|Uad+Z*&$L_IvT3-nX33ksECm|tmE_QEP+P%S3HP}6NkKJSU*gbZS-DCIIJ*}^Vgao^1ij$C#I2XGY#qvV$O&+U3@6mho'
    '9=%8J(R=hBy+`lqeI+C$=si=MgoMP|=)LM#?{%BXb2aEadXL_t_vk%(kKUvA=smr!goFgWXNr@MkT@H?S1<Km?ZF!K9=%8J(R=hB'
    'y+`lSd-R^(S3*L9-ZRBXNJyNG-fNb6uk~aNdXL_t_vk%(kKUvA=skK*?<*l8LGPL3BqSuxM(?#tz1MlP2E9k`(R=hBy+`lSd-NW?'
    'r}veRkf8TWaS{>|XQTJJrQRDnT7%xB_vk%(kKUvA=skLm-qZU^NJ!9orZ@=+iL=pr{Zj8u9<4#|(R=hBy+`lSd-NW?NAKx<B_t&1'
    'JyV>7gv8nCy<wsEvNxjl=skLm-lO;EJ$jGcqxa}Ny|09X1ifd9laP=&6TLSs^<M4K8uT8$NAJ;l^d7xO@6mhop59kNLW15i#Ysp='
    'oQ>X_mU^%8XbpOg-lO;EJ$jGcqxa}NdQa~wAt6EUnc^fQB+f<eMa8+$d!0vX(0lYAy+`lSd-NW?NAJ;ldS3|%33|^ICm|tmHhQmG'
    '>b=3EHRwHhkKUvA=skLm-lO;EJ-x4lgao~3ij$C#I2*lJFZJH&(His~y+`lSd-NW?NAJ;l^q$^VLPCPxGsQ_rNSuw{YZiJhi*tI9'
    '-lO;EJ$jGcqxa}NdXL`I`$|Yi(0ish2?>ca(R=Mu?^PbHLGRIf^d7xO@6mho9=%8J>3t<6B<MX;oP>nL+33A)srMR>)}Z(3J$jGc'
    'qxa}NdXL_t_w>FJ5)$;DDNaH{;%xL@ztnr3M{Ce~^d7xO@6mho9=%8J(R+Gd2?+^$&lD#iA#pZ(Z&>QR-lH|>J$jGcqxa}NdXL_t'
    '_vk&nuY`mIy=RJ(kdQbVy*DoP-ssU9^d7xO@6mho9=%8J(R=is-d93Gg5ER5Nk~YXjozD<dT;V*4SJ8>qxa}NdXL_t_vk%(Pwy)s'
    'Awln%;v^&_&PDG<nR~4Fn#!X!=skLm-lO;EJ$jGcqxa}Ny|09X1ifd9laP=&8@*R8^<LxA8uT8$NAJ;l^d7xO@6mhop59kNLW15i'
    '#Ysp=oQ>YAmwK=DXbpOg-lO;EJ$jGcqxa}NdQa~wAt6EUnc^fQBu-oJ6~T8u>$}g}{r&baio1M<Zv5xN?EH?@UH)<Rw~xD<@~3yv'
    'u8h%43|Zet<JM12G(^+bMLXNKV>*VJZk(boiDEt5l}$HnqVSDYul$bS(5J3zOI;lQ5#?~qs(<~odwPD&f8gW8-;ck2|8IT!=$6_v'
    'mh})dbrjK?y3clGs-rcki*wxk{!jk4efae$4qzPF?e)X%YQNjw#-FSD%L~8l@1J+GJKcZyRQ~hb7kT-I%$)!2RFp+g#CvnQ%V2a?'
    '(bzSm-6>5|wm8{DrXD%|V)hF4mtgEeT{_8WHbGN<W$H?MwfXOI_*t(Wc8@<l=bawg_3Hj<yWhkyNRwNoF^%I~chR84sr$Tr{`6GN'
    '%G=%Z_Wohk#-H~4-Q)AsF8&yQ|7mv2V|1Zy+e9mRWSyT&A5-P~Fw|qTYG0i5RbIota*pw>cKgrwH{X15iZ?TU&d2-bG8$e-R!2h_'
    'Z*zHf>#<0O!@EAVZ9la6(+r!b>!Q)`{#O<|JGfV~*nhm%R`KRli+|%nP4*wJ#at5eLc?aW*=#nO&1SRNZ1xG->_bKQiltqAxj(bg'
    '&MzqV1qCIYl1@pdq*Kx<>CUXAd*$CPRnw{I)O2b(HJzGHO{b<))4hI8cfEc5_3C<e_hDaFyT=@vQ$3%peLufTZTaEj-_Bu_C;U*&'
    'Z*)0qIf`=-=6OBT^ZVJB9nC93j<_5f!!Vx{afIUy$g?#EY>t6poZtJd9F{!aa&+c68k;f~S2;`Nd7VQ$N6XmGXGtEwJTLPN$ZOX)'
    'm3Mi_`7Ey`d0cZ;P33n7;_Zm{FVEi`?KvuPJ-%Eyv!KY+G>2;rzo{=%c)WG-PUo4P^Fm+G*Os!JpL1BA&^fUCVLqYrEZOADSgNO{'
    'jI?szw&k6T_aiHWoC1bsKK1i_>a#W}lT!}$p`B0hwwwliKTkqAT@2GaOyVbsw;%^_PGmVPjGK8lbmgSUNuZo|eHo}Zq?$b0a;TS+'
    'wJJkB2Tq%(S56i=vE>vrRb}W+Ios!ykOw~}nyD@WERKI1{=7ox4489Oj{K&KfH?j+dE_LTQ&L`t^MvU0tj*D%b7lTgM9vXS87=V+'
    'l-2G{nY_v%&p}(~g`?D#U8yC?Y|@rXR?c8K)#p1fbn|?W*UB-kGG)fj8F=XDt9Vx~a5>xMWg+L#v6|<O_>tmW$XP9C_pE%zx?J%$'
    'c`?j+F)yV#+h$Wz=TMG!AZNd-ohzhqEOSYp#c0l-IoIdBF*Wn=hEzFfbDGS{MNYm`TPB@&1LFASOq*AQoWFB6ET=?{@SITd!qRv1'
    'gqmlB5otskk*6~v-yg1-+q>u8-X+rjH9%c5jYuQXi2Tka^W&%Mhx?~rcDGmO^n2BJ_mA6$tH<s0{pbAs>h0Zs`T-=IJ@v4OkvWZH'
    'Gj>Bg=2UR}N<YS6aFi~V_w%QE?jLshtHY;lp33^yYkc13{o&I@PxGh7=bH&FZWUfW{&1@I5-LAm)<V{Mt@m2*wccyJ*Ltt@UhBQq'
    'd);J6LPA2qO@<^SBusJNzlV6e)PDVV+S;$I7t(&TAMHo`(SEca?MM63ezc#2goFg`Cm|tWiu?Yf1DDz_x~h}beq&Ah(SEca?MM63'
    'ezYI$NBhx!x&R3Y3EEFWLc$dHlJ;vC+HcxyPFwqx4vqGs{b)bhkM^VeXg}JI_M`nIBqStgKM4s5Q`~p8U$@kLS^b@`_M5VKrTu6>'
    '+K=|5{b)bhkM^VeXg>)F2?^RyLPEk6_kHcxFSTED+K=>@(qPbjv>)w9`_X>1AMHo`(SEd_goK0y?I$52VT${{_8XSkuRZNYdQ7cH'
    'deDBfAMHo`(SEca?MM63e!2h&2?^RyLPEk6_oDV2m)fsC?MHe{y$|`)ezYI$NBhx!v>)w9`_X>700{{R+D}44!W8$S_M4X4Z#?Zs'
    'dX%rxezYI$NBhx!v>)w9`_X>1pM->j1nnmwAz_O9zV?e^dEx!06Mv@1<k$UpKi-e`<NbI)-jDa={dhlZfP{nu?<XN4VTya%`&Gx@'
    'uiI27{ZNl?lPwwV$NTYqydUq!`|*CfAMeNeNk~XY@O}~!5~jEpykEWae)V|T-mmsl58jXW<NbI)-jDa={dhm#Pa7a1A;J4eNJyCC'
    'UiN;?()+dTX?wr6=KXj--jDa={dhm#kN4yKct34`goFg|Cm|tWihI%fwM*~UZ%*6$WkbXJ@qWA?@5lS`e!L&=$NTYq5)u*;yq|=G'
    'gemR?@7FE8-_W17_ZvLdgZJb8ct75c_v8I|Ki-e`(*{ULNbr6V5)!7km%U%V^nTNcKi6aQTo2xl_v8I|Ki-e`<NbI)-cK7KAtAy0'
    'Nk~YT;$HTC!@~PzBXi2$uk>!bAMeNe@qWA?@5lS`e!L&=Cm|sr!TU)_NSNYY@P6ac`_-rYT#wR%@qWA?@5lS`e!L&=$NTYqyq|=G'
    'gaq#=At7Okd%^onOYhg7_H#W-Tg3bEe!L&=$NTYqydUq!`|*Af5)u-;pM->jDen8;FDlN3_v=pkxgMpd;r)0&-jDa={dhm#kN4yK'
    'cs~gV2?^d$LPEk6_k#DUmfmkT?dN)w28Q?J{dhm#kN4yKct75c_v8H}BqStwKM4s5Q``&QuU>k;@x-6&k?jZX$NTYqydUq!`|*Cf'
    'AMeNeNk~XY@O}~!5~jEpykE2Mepy7HviGYup6kK;@qWA?@5lS`e!L&=$NOmmBqStwKM4s5Q{2nmuU&e-`m~?xk>x+{$NTYqydUq!'
    '`|*CfAMeNeNk~XY@O}~!5~jG9ykEEUe$8n=*CR`E-jDa={dhm#kN4yKct75c_mhy2kl_6!BqU66FL}Ry>HWIXey&G0y}Tdq$NTYq'
    'ydUq!`|*CfAMYn2AtAy0Nk~YT;$HH8!_xcpr~O=yI$IOokN4yKct75c_v8I|Ki-e`laP>*;Qb^dBusHHc)xM!{l*i2uE*$ezPumr'
    '$NTYqydUq!`|*CfpEf{3LW1{`kdQFNz3ly_h4*VVC;nWI$#Xq;Ki-e`<NbI)-jDa={dhlZfP{nu?<XN4VTyav`$d_%@P5^4Ki8wF'
    '{JtOW$NTYqydUq!`|*CfAMd9PkdTnz{UjtLOmQ!JziR3In$v!+M^k&Q2k*!G@qWA?@5lS`e!L&=rwx#hkl_6!BqU66FMGdw>HXT%'
    'ey&H8tq||W`|*CfAMeNe@qWA?@5lQ|NJvQVei9NArnvvf`xWzde_IsZ=k5M}`xwPtKG`?^^I?{OM@}#Qxcl43-A(z^yJ%O&XeNfN'
    '@1t?+rzRSrY3!n%?b|Wy=6F5(k8z5=B#QNHS2o>HM+*_HUiq!UAsW6YTT5LW{}JVI%&LF=w0nAf&41wI!{3j;egAKL`{<V1RGIY<'
    'HFXrxo4U_-WU8Yzs-yqOH{#1b`P=s4*QYpuab&mG54)@VZhISluIeu@{I<V;-px{V|KU^l&v(E3%uQGKL;d@|ov)AC>co3<yUSp7'
    'R?*lsrQIn_Q?@wSM5Z1&{$lnD^_O7mLtQ$_X*NMqer4)Pd$sxRa`;)V9(IpEKj+;f+x6=HX}jOVF-XN*r7?};TzAo+#Hstdeg5=R'
    'Z1e5zd3*mb`{Ymi{qFJkY8QWuzyCDL=P|m_wr!#nJ<`$7rH`rdeTYL=9QG`uuksrHm2-@5wcCHbzxn2iQ@ok+b3Wcbm(lQQ7P}qW'
    '`L|c6e9sQ=dYo`^_C}N3k6k+q-MAV4HxfI~kk^ygf4tUK@!nO7f8#<$_8+gsR1(udLuQlNWHy;iW|P@u_6f@DLqYkvzn?IiS7qnd'
    '6a0FDicUqRqEpeS=u~uPR?+>CUpZCMDe07SN;)N-l1@pdq*KzpVo7(sef;(6dUy9>UzWOu{3oWv8QXXB`<cgblS4MgS~H(Q<?YJ@'
    'Tn2KEuN(ox;XRGx8Hc$)jJiDIhjE@;^C(X_C6vLM<6s!(88>z>j!d4%IfUE!%q(X`PElonewwdQ<9w!6Id2Vzk({Gw+LU*=%>$d~'
    'X>}M`<5b@Ds?1_}k*N>M@>5+-vv`ZTGVhdmAjiox&*O2j#n~_&CWpS7Puv`mea?e(l{ByP=T#@qq%u3^sh&f9I9!VI{ONPHD>YM7'
    '#$Gw^^Q7u>4lFZMj`yLNM?;>8W0oytzG~+YSkC_ViJDT)l=&%Vknu1k;zx=%VK~fMIb)1{8Bd!$n{rmD4ztMEl`&RMv77^P4(#Sp'
    'og*sVfVRxFWy;G*X_|-GlvjZ9Fb(Dulo#AQBkDXybClPIc`4_FJR{1Po6}0Z0sUd3$U$1?H6-Vnd;^BfJi>G2R#~r2hpDxn=3$>B'
    'ugyzJnV6e-vMUo!{6I~)(v+(}PL;!9`iY+?-h|<BZOF-atmla%ex!I8assRlQ}$Su>7-l`bGpk>+s)-jP9A*@#GC^28r#jk^^p@w'
    'm!oq$ly5oNPHmZr#;j6vLah%I@YIy)sVcQ=&YyYx=;s+RXQOxnx^lfLmy5oiC)0957?DP#5qUBr^8MkOxxIVd?OierPy^H@(}*-8'
    'jmYm@GCzL0ez<@7Wp{gZPQO%rcmKG3xO&_^-+#_uuHN4Lr=LE;*;5ajxCTt)*o@`#0?qQ3evDD?C|xY?=a2N<KkW8bhmYDkmG!UJ'
    '_^{3U!$*mp=8ufew-{R7S-gDq;a}Wgs5*f=47Fp{LY8|i_ge0?+-tela<AoH%e|I+f6xtv;t2HJ7&}@WZo~1V6k9DmtDCjX(0{o#'
    '$NVpLU4F{=%RM^2i7xKgp>R|<DjXG#3P**b!cpO<a6GfZ(e2tKBqT27F%9){4J9szC%uN!`^cPDSSzd*)(UHdwZd9qt*|u|g`>hz'
    ';izy_I4T?!jtWPGqcs#xSVBVLhdk<fxrP$e#!0WC<YieatQFP@YlXGKT4AlQR#+=+4MpLoa8x)d92JfVM}?!pQQ>F}g%g&Lka+bq'
    'ly<p>5*MzMUPGBCt*};DE36gP3TuV6!dhXiur(Bgqry?)sBly`DjXG#3P**bH55)*LPFvPtf6#^HIykXCa1iHGUb&;E36gP3TuV6'
    '!dhXiuvS<rYz;->sBly`DjXG#3P**b!cpO94TTeykdS!wHI#n2hEkvQGv%i&8MMM$VXd%MSSzd*)(UHdwZhg=6pji<g`>hz;izy_'
    'I4T?!j@D2(VF?L|S6@RJmTM^OX+KkbYCRfME36gP3TuV6!dhXiuvXX_io#LhsBly`DjXG#3P**b!qFNECoCZ$@gvqy#^oAHf7;KK'
    'pR%OZ3TuV6!dhXiuvS<rtQFP@TSHMeDjXG#3P**b!cpO<a8x*2L*axaBqUya4P{!cp$w<}O!;Z>Xi%-NR#+>n71j!Cg|)(3VQVM~'
    'M}?!pQQ@d?R5&Ue6^;r=YbczsgoMP8SVM^`$YK#?I`N0fPo52`7uE~wh4sREVZE?kSTAf5Md7G$R5&Ue6^;r=g`>hz;b;+s6qb;X'
    '_#ulZ)$t-qx7nQZQ{}r&<>8=uVZE?kSTC#>)(h)}^}-fW6pji<g`>hz;izy_I4T?!juuf!VF?L|AF_y2FBehj?z9(CYEK8%3+sjT'
    '!g^u7uwGa%tQWS3qHt6=DjXG#3P**b!cpO<aI}a*3QI^x{E$VIX1R#cR;Rs)k~M-}STC#>)(h)}^}>2#y|7-`B8tLM;izy_I4T?!'
    'jtWPGqr%Z53MnihA@TZ)DD83)r5jFr5vB8dP`$8TSTC#>)(h)}^}>2#izo_5g`>hz;izy_I4T?!jtWPMD5S82gv1Y7MCq1`C_{7F'
    'iztKVgX)F#!g^u7uwGa%tQXb`TSQSfDjXG#3P**b!cpO<a8x*2L?MMGBqV;wB1*qpL>W)~!Sa*mgX)F#!g^u7uwGa%tQXb`TSQSf'
    'DjXG#3P**b!cpO<a8x*2L?MMGBqV;wBFeB>M9C}sDKDaw>O(KA7uE~wh4sREVZE?kSTAf5Md7G$R5&Ue6^;r=g`>hz;b;+s6qb;X'
    'c>P6`ak+?6pZ0_0OQoR~)(h)}^}>2#y|7+bFRT}~h@x;*I4T?!jtWPGqry?)sBpB1LJCVrNWA_c%CuZWX-@mW@};WK3+sjT!g^u7'
    'uwGa%tQXb`TSQSfDjXG#3P**b!cpO<a8x*2L?MMGBqUya5hbSK#Ue^~+7Fg5HGy7OFRT~V3+sjT!g^u7uwK|Aio#LhsBly`DjXG#'
    '3P**b!qFlMDJ&r&@%oD>)p8M~KkWz0m+Q1%STC#>)(h)}^}>2#y|7-`B8tLM;izy_I4T?!jtWPGqr%Z53MnihA@TZ)DD`p?Wjyf*'
    '%jdOIFRT~V3+sjT!g^u7uwGa%Y!OA_sBly`DjXG#3P**b!cpO95rq_%kdS!&MU-Z-h?2G9DKDawo=Y#R7uE~wh4sREVZE?kSTAf5'
    'Md7G$R5&Ue6^;r=g`>hz;b;+s6qb;Xc>P6`cDaaBo%Vy}^D3to)(h)}^}>2#y|7+bFRT}~h@x;*I4T?!jtWPGqry?)sBpB1LJCVr'
    'Nc@0Bly13*(wz2#<+F~|3+sjT!g^u7uwGa%tQXb`TSQSfDjXG#3P**b!cpO<a8x*2L?MMGBqV;oB1*qpL}^d^!SZ=M(hKW_^}>2#'
    'y|7+bFRT~V3tL1{I4T?!jtWPGqry?)sBly`T0|j*B_t$%z#_`9Ttw+l`@!;c@A;s5VZE?kSTC#>)(h)}^}-fW6pji<g`>hz;izy_'
    'I4T?!juuf!VF?L|AF_xtE*DY86MwLLR>OK>y|7+bFRT~V3+sjT!g^tgC<;e~qry?)sBly`DjXG#3P+15q_Bj9#Op7jOv^=->BJu_'
    'KY2c=URW=z7uE~wh4sREVZE?L6osS0QQ@d?R5&Ue6^;r=g`-6jQdmMl;zul^#N2<ph|*N2{b2c~@_bOeuwGa%tQXb`>xK2gdSQzw'
    '3P**b!cpO<a8x)d92JfVM~f(=u!Mxf4_QR1mWwF$X+K!LX*?fPFRT~V3+sjT!g^u7uwK|Aio#LhsBly`DjXG#3P**b!qFlMDJ&r&'
    '@k16->g6Izd)g0{Z(7d>)eGx|^}>2#y|7+bFRT}~h@x;*I4T?!jtWPGqry?)sBpB1LJCVrNW9)6N?As^zg_**tXH46`}^%<w93PJ'
    '^&J2CFt4l}Es*3NcYpi1yD5K)>bV}{3OnRQFRGb-YNAS*#x82SK6<&l4pd1O?Nt+v>L#z@n`olq`V#d|`L)0ys-T$5%f%x8Bd!rK'
    'tN!)V?&<k8|ACJWe?R{A{lE3?ql9R)y|0J3(8QHxQ}=m|o}x(Z>nIBHjrj6U{<eMi^{E`?=k4{w?rOi=-sXB#{pE$<_V>@br}?V#'
    ';Zym~cVBw^?D_kl{{7$1*Q2X^pYP4>E`w2SM77hD8lY4XdEL*-Dtm_GFXjcb{t}FRs7rY@&F#^YUzxg6b8P;*9DdfThu!1P&-nzN'
    'cD=fP+U_@T3{rbksS)Bh*Iit-<J5iLK7X3m>uz_?+xv%kaqiQ8zk7VX+QlE^??25ed1I7jZQDeBcC@zlTrTcaz7In^M*Z?-W$$WX'
    'dFDnuzSVC3`TpjcFGeRH-|PPI{<(~XS2H{DHfIIiHJh&g-RxBVE00rtir4Zuf4tU4wck~Xf8#=3&L6ME#1WG~!{_ihd=8((=kPgv'
    '&Iv9A9?HkpU7_nvxsJ0QXFX2+rhZeuso&IZ>bEni-+svN<}2V7a0)mDoB~b(r+`zyDd7HV1>E)a@z<;C-Q9<MS+p)AJKxhhxBJ6!'
    '&m&ajsLv~0p2Gb!PqBGC`W)!f;Y=Oc`Q49$6?>8AWe&||K6lEyoO4?l&t)9uh#3y=W*ql;Tl&Lz%aJiw<-Kn5SmimmIh_AvU*_j>'
    'bn;x!iLaf{$2@boJaBnN=g7_xFwW;koIdeBkB0%ALv3p275OeFwmhrr!-&f3NM29NLC=#n2XTKm#dD|+`G%Bb*i9K#<^0ccuFk1#'
    'I!p?~W<JgHgdDOA$$6-mM`<~m%Xyb`WSMVrW*83Fpghf{@*52~spSkX4D+appD5mg9PgXMb$hJm(Gx#Xya_q3bAD^*D`}3BD(Cqe'
    'y?HX`d^65tq0durI8+{a-j7omC-DZv@z2X~b(lP+whYKpcjZKp6J~c9%w<x`nk?sooELKjo959KZ$Z2R<6-*i`}y)*hF+e`IVaTf'
    'd{kzR9BAdTkh6Ne0sUcu$f4Y=`@<Ao-hpPGKH>+8_aG;>&0$g<#(A2EpD5mhVKYzU?L5tui+lV?O}XNft3^(&<6+{-VK*Eu3|S71'
    'U73`IyhP??Ssf<oX;Y@2a#74FF)t0>JaOg(Gv)O%C%!!RIaN<{Q4q&J4*z(#(&UVsb7!6rQ(hHw`mN_Os!Wl2LKu-oq!D>KBl7*>'
    'nz_Au-tAp74NwErCDVvBB8|xJTrxj?x_-ES`ek=}bxyw?eRu!3eYkquKHq=NUz6V6{imNJ!r4;~o0!|CacstJsK=a3k6-D>m`xm|'
    'i{<_N;hg)2-TvzEp_!+${`DFkn|XiuP|(wSD~<WiYm2AXlut4IiyN<PPT<CC@$*#{cP;K(+_kuCao6Im#a)ZL7I$CiHf!#hAt530'
    '2Jf2T#y}Dh5*PAxg?j1uqDMGs$JdV>AIHb>aeN#f$H(z;d>kLgCm|sr!SP8*NSNZ5Pf%StzOg=S$2a;tK8}y$<M=o}j*sKx_&7d}'
    'PeMXMg5#5rkTAt99bdb2eA9T+j&Is*I6jV#<Ky@^K8}y$<M=o}j!!~DLW1LykdQFNEgWCBaC}pgp(pM5vIge(I6jV#<Ky@^K8}y$'
    '<M=o}2?+@aj!!~Dg5x`KeErh#HK+Z|jVXI6j*sKx_&7d}kK^O`I6jV#<CBn(kl^?vBqTV#BgZ!^9bbFe&)k?=&)nenI6jV#<Ky@^'
    'K8}y$<M<>bBqTUK2?+^P+|u!lOUKur_A@u8-ZM8iK8}y$<M=o}j*sKx_&7cZ2?+^~PeMY%6t{GI)6($`C;rTh>_j*|j*sKx_&7d}'
    'kK^O`I6jU~LPA1<<CBn(;P{RlUlhv=$v2(&LpLT5-5~i$K9Y~*Bl$=^l8@vg`6MJHBuG992?<l&SCX$fmVDi&I_alw#Lrigd?X*q'
    'NAi(;Bp=B~@{xQbpM->j1j#2MAz_MJO1^q2`Re|(C135a8zdjeNAi(;Bp=B~@{xQbpM->j1j#2MAz_O9O7b;J$=BAWE%{o{-5~i$'
    'K9Y~*Bl$=^l8@vg`6MJHBuG992?<l&SCX$?O1^G9ZOK;@9mz-Xk$fZ{$w%^$d?X*qNAgKXNJx-;5)u+5-;w0&mXdF1Ph0X09=t*F'
    'k$fZ{$w%^$d?X*qNAgKXNJx-;5)u-oxUVE%zm$B_i9dK_^56}UkK`lyNIsH}<Rke=K9WyDLPCP%laP=w#eF6DhK1zIw&#>3-`s4G'
    'd?X*qNAi(;Bp=B~@{xQbpM->j1j#2MAwlvTNxpF@`Rdbt@J4CBNIsH}<Rke=K9Y~*Bl$=^l21ZHLW1OzkdPqxjwRo;lzh!;KX{`w'
    'IwT*-NAi(;Bp=B~@{xQbAIT>nAt6EXNk~YLd`FTmD$a%E>rVT@8>Lkt`A9yJkK`lyNIsH}<Rke=J_!j436f7jLW1NwmVDJx@(ri`'
    ';EmF}kbEQ`$w%^$d?X*qNAi(;B%g$YgapYaAt6EX9ZSA?Dfz||fAGfW!5btW$w%^$d?X*qNAi(;B%g$YgapYaAt7Ok`%3aP3(1!y'
    '_bE%hdgH+xBp=B~@{xQbAIV4Zk$fbdgoK0y$tNKpVT$`o^0iCJSDp5QH?s66`A9yJkK`lyNIsH}<Rke=J_!j436f7jLc$cckbK=z'
    '@-?UZ;EgQDNj{Q~<Rke=K9Y~*Bl$=^l21ZHLW1OzkdQFNEhJyRlziQ3KX@Y>UXqXGBl$=^l8@vg`A9yJkK~h(kdPqxBqStEaSO>e'
    'EG1um+7I5Svn?U{NIsH}<Rke=K9Y~*Bl$=^2?+@al21ZHg5*1veB)B`jVJ!#jcoo&K9Y~*Bl$=^l8@vg`A9yJPeMXMg5;BskRbVv'
    'CEv7^eA8(^c%#{P@CM08@{xQbAIV4Zk$fZ{$tNKpAwlv<NJyCCmXa^Z++)etRHyymji&P84U&)KBl$=^l8@vg`A9yJPeMXMg5;Bs'
    'kTAu4CHbnQ<ZDj*!5dBE!5btW$w%^$d?X*qNAi(;B%g$YgapYaAt7Ok`%3cFOUc)s_JcQ?Y;Z_El8@vg`A9yJkK`lyNIsHJLPA1<'
    '<dcw)Ao-H@>i#d=pLb6&=d7Yc+(gB?*>;!7=I}rE-~QWw`wsvB0000000013|2ki)>au_Tp{g%@|C`I+e*gdg000000001Qht+yj'
    '*KPseBhdEW{@Z^50000000000!1~vDQ(yQk0O||>2z=eT1poj5000000001hx4~+?YFf7d@DXVHZ~yH-00000000000AT&=ylF1{'
    '5eUtN-vXei+yVdq0000000000z}sN8UbVGb0Qd;B{kQ-29{>OV00000006N5b>6fW{t@u@!fyf4wr&9c00000000000N`z~TCX~{'
    '0PqoL`)~j4KL7v#00000003b9>&)pc{1yP+h2H|8a|-|f00000000000B?lVdeygX0pKIh_TT>7e*gdg00000006-H*Ll-l_#+Vd'
    '3%><GU%3SU00000000000D!l_YP}k2w*c@FX!~#f?LPnj000000001B{p-9LF8m|l!-d}hU})U}0000000000006+-V6|S2ZUNvU'
    '(DvW{+kXH6000000002M`q!B=Uie4g#|!@m{8+gK0000000000004lu!D_vlTDJi35or5w|Ls2j0000000000VEya7nJ)Yh2-Ah%'
    '0%39s00000000000001Qgw=Yrt=$5^N1*M${kQ)B0000000000fc394=h8m{e&M$O*tTu~0000000000007`^uv)LK-2%WzpzXi?'
    'xBmbD000000001h^{+GM`oeDkaDCyo0JwGw00000000000001Qgw=X=;}!ru0&V~8zx@XQ0000000000tbd(3Hy3^jfSU`y1;CA4'
    '00000000000001ZBdpe|+tw`rd<5G5+kg8H000000000009gMzZ*DLA5eT;zehYxx$}Io@00000000000K5%W>(#Dy3jiO1w*U6u'
    '{sRC2000000002izs{T8g?|Kmcj31H*tKo}0000000000007`^uv)L~+ycNypzXi?xBmbD000000001h^{+GM?!s>YaChOi0Jw7t'
    '00000000000001Qgw=YrsW+8d0N8)~Z~yH-00000000000AT&=toOgV@ZW!EE`0yn%iezg00000000000B?uYdR4gvfR8}ifBSF$'
    '0RR9100000008S>XHIqDAAzqf{3GyH<rV+{00000000000Nw_x^{RFY03U(2|MuVh0{{R300000007p%&Yb$fKLTD~_$?6X)-3=4'
    '00000000000K5%W>s8ab1%Qu0+kg9S{{a91000000002%U*}D8;kN*2F8mP?P2&~-0000000000007<wtM#gN3jiO1w*U6u{sRC2'
    '000000002izs{WY!aoAvUie4g+sZ8f0000000000006uVR_j$)y9I!cK-+)&Z~p-R0000000000>tE+hci|ra?=Jin0A1@A00000'
    '000000002q2CMa|Z`}gGN1*M${kQ)B0000000000fc3BQroZq<AoLf03xK|I3jhEB0000000000Z-do(HPmha;3LrX-~QWw00000'
    '000000Kod!c{5!2N5F>*zXib1x&;6L0000000000fVaVFy&ByDz(=6%zx}uW00000000000D$$cGiSW;TL6p~ehYxnEdT%j00000'
    '00000yb)IG)#MfcJ_2q3?Z5p800000000000IYwVIn#yT0${rETL4UM0RR91000000002sjj&p;wr&C7BhdEW{@Z^50000000000'
    '!1~v@bK$oD*k1T80Jd%c0000000000007{Puv)LK-2%WzpzXi?xBmbD000000001h^{+GM`oeDkaDCyo0JwGw00000000000001Q'
    'gw=X=;}!ru0&V~8zx@XQ0000000000tbd(3Hy8d9_?rv=2>eau761SM0000000000-Uh4n>eej)d<5G5+kg8H000000000009gMz'
    'b8avEBjC3eehY-#)-3=400000000000K5%W>($OJ0DJ`6{@Z{14*&oF00000003D3I&*dxehYxzh2H{T=N13}00000000000Nx0z'
    '_3F+o0DJ`6{@Z{14*&oF00000003D3I&<zW{1yOr7k&$XJGTG;00000000000Psdwtyi0NQ@aI#{kQ-2-~Iyt0000000000*1yhr'
    '|LY6?{fFkl_rJRA{RaR5000000002+c38b%ubw`9+TZN1u6K7I_Pf<j@2aL-H|wey*7dsHv^JaVLN;UHW;6C351Y-{x7lnqo6Tmk'
    '*=#nO&1SRNY&M(CX0zFBHk-|6v)ODmo6Tmk*=#nO&1SRNZ2za*A9n!Q-aYU3ZUA7j`5>UNZ?hTuHk-}Zx7lnqo6Tmk*=#nO&1SRN'
    'Y&M(CX0zFBHk-|6v)ODmo6Tmk*=#nO&1SRNZ2w0$0C?VBKkS~au6K7I_Pf<j@0w}d)awlZ0000000000001~^nrYpRjspMy00000'
    '000000O$W30DbE?000000000000000000000C3_|n<oEn|J6(J!s500VS$U6000000KnPte!Y6$UO()fuC{m2yZ!2?cXQA0&;S4c'
    '0000000000aNac2x*Z({00000000000002a|2F{o)^Pv;0000000000000000002s#Hlt-{@?zqm*R!RYxBbb7cT(-0001hv*Z1G'
    'b@SmbfBEpZ`sp9}Umx!ucDJjas&2j7ef)6q%j&15Ua$7M?d{)JKfR0p{_?-^7jHj2?!I~P>G{LQkMp<q=Rf~HpI}j4'
)


def native_post():
    import base64
    import json
    import zlib
    return json.loads(zlib.decompress(base64.b85decode(_NATIVE_POST)))


class NativeOrderingOracleTests(unittest.TestCase):
    def test_actual_postcommit_native_fixture_and_replay_corruptions(self):
        result = oracle.audit_development(native_post())
        self.assertEqual(result['native_entries'], 128)
        self.assertEqual(result['native_faults'], 32)
        self.assertEqual(result['exchange_chains'], 32)
        changes = [
            lambda r: r['owners'][2]['trials'][0]['fault'][0].__setitem__('resume', oracle.CODE + 4),
            lambda r: r['owners'][2]['trials'][0]['fault'][0]['frame']['x'].__setitem__(4, r['owners'][2]['trials'][0]['token']),
            lambda r: r['owners'][2]['trials'][0]['captured']['x'].__setitem__(4, r['owners'][2]['trials'][0]['token']),
            lambda r: r['owners'][0]['trials'][0].__setitem__('faults', 1),
            lambda r: r['owners'][2]['trials'][0]['fault'][0].__setitem__('esr', r['owners'][2]['trials'][0]['fault'][0]['esr'] ^ (1 << 10)),
            lambda r: r['owners'][0]['reply'].__setitem__(9, 0),
        ]
        for index, change in enumerate(changes):
            r = native_post()
            change(r)
            with self.subTest(index=index), self.assertRaises(ValueError):
                oracle.audit_development(r)

    def test_requested_identity_is_bound_before_acceptance(self):
        r = report_fixture(9, 4, 2, 37)
        expected = dict(expected_profile=9, expected_vcpus=4, expected_rotation=2, expected_seed=37)
        self.assertEqual(oracle.bind_requested(r, **expected), oracle.configuration(9, 4, 2, 37))
        for key, value in (('expected_profile', 8), ('expected_vcpus', 3), ('expected_rotation', 0),
                           ('expected_seed', 1), ('expected_seed', True), ('expected_profile', 9.0)):
            arguments = dict(expected, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                oracle.bind_requested(r, **arguments)
        for bad in (None, {}, dict(r, n=True), dict(r, rotation=4), dict(r, seed=0), dict(r, profile=13)):
            with self.assertRaises(ValueError):
                oracle.bind_requested(bad)

    def test_all_declared_profiles_and_role_rotations(self):
        for profile in range(13):
            counts = [(2, 2, 2, 3, 4, 2, 2)[profile]] if profile < 7 else range(1, 5)
            for n in counts:
                for rotation in range(n):
                    with self.subTest(profile=profile, n=n, rotation=rotation):
                        result = oracle.audit_development(report_fixture(profile, n, rotation))
                        self.assertEqual(result['native_entries'], n * 32)
                        self.assertEqual(result['native_faults'], 32 if profile in (8, 9) else 0)
                        self.assertEqual(result['exchange_chains'], 32 if 7 <= profile <= 9 else 0)

    def test_raw_fex_and_adapted_images_are_not_confused(self):
        config = oracle.configuration(9, 4, 2, 37)
        packet = oracle.reply(17, config, 2)
        raw = b''.join(word.to_bytes(8, 'little') for word in packet[9:13])
        self.assertEqual(raw[:12].hex(), 'c480e4b81f2003d5a4fc9f88')
        image, length = oracle.code_image(7)
        self.assertEqual(length, 24)
        self.assertEqual(bytes(image[:24]).hex(), 'bf3f03d5c480e4b8bf3f03d51f2003d5a4fc9f88c0035fd6')
        image, length = oracle.code_image(5)
        self.assertEqual(bytes(image[12:24]).hex(), 'bf3f03d59f3f03d5df3f03d5')
        self.assertEqual(length, 36)

    def test_forbidden_outcomes_rejected_even_with_consistent_full_frames(self):
        cases = [(1, (1, 0)), (2, (1, 1)), (3, (1, 1, 0)), (4, (1, 0, 1, 0)),
                 (5, (0, 0)), (6, (0, 0))]
        for profile, values in cases:
            n = (2, 2, 2, 3, 4, 2, 2)[profile]
            r = report_fixture(profile, n)
            slots = {1: ((1, 4), (1, 7)), 2: ((0, 4), (1, 4)),
                     3: ((1, 4), (2, 4), (2, 7)), 4: ((2, 4), (2, 7), (3, 4), (3, 7)),
                     5: ((0, 4), (1, 4)), 6: ((0, 4), (1, 4))}[profile]
            for (cpu, reg), value in zip(slots, values):
                r['owners'][cpu]['trials'][0]['captured']['x'][reg] = value
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                oracle.audit_development(r)

    def test_pre_and_post_commit_fault_classes_and_precise_retry(self):
        for profile in (8, 9):
            for change in (
                lambda t: t['fault'][0].__setitem__('resume', oracle.CODE),
                lambda t: t['fault'][0].__setitem__('far', oracle.PRIVATE),
                lambda t: t['fault'][0].__setitem__('esr', t['fault'][0]['esr'] ^ 64),
                lambda t: t['fault'][0].__setitem__('esr', (t['fault'][0]['esr'] & ~63) | (7 if profile == 8 else 15)),
                lambda t: t['fault'][0]['frame']['x'].__setitem__(4, 0xDEADBEEF),
                lambda t: t.__setitem__('faults', 0),
                lambda t: t['fault'].__setitem__(1, copy.deepcopy(t['fault'][0])),
            ):
                r = report_fixture(profile, 3, 1)
                change(r['owners'][1]['trials'][0])
                with self.subTest(profile=profile), self.assertRaises(ValueError):
                    oracle.audit_development(r)
            for bit in (7, 8, 10):
                r = report_fixture(profile, 3, 1)
                r['owners'][1]['trials'][0]['fault'][0]['esr'] ^= 1 << bit
                with self.assertRaises(ValueError):
                    oracle.audit_development(r)

    def test_complete_memory_state_and_machine_controls(self):
        changes = [
            lambda r: r['owners'][0]['trials'][0]['captured']['q'][3].__setitem__(5, 0),
            lambda r: r['owners'][0]['trials'][0]['captured'].__setitem__('nzcv', 0),
            lambda r: r['owners'][0]['trials'][0].__setitem__('actlr', 0),
            lambda r: r['owners'][0].__setitem__('actlr', 0),
            lambda r: r['owners'][0].__setitem__('isar0', 3 << 20),
            lambda r: r['owners'][0]['bank_after'].__setitem__(123, 0),
            lambda r: r['owners'][0].__setitem__('table_writes', 1),
            lambda r: r['owners'][0]['final_native_leaf'].__setitem__(258, 1),
            lambda r: r['source_after'].__setitem__(3, 0xCC),
            lambda r: r['tables_after'].__setitem__(0, 0),
            lambda r: r['owners'][0]['request'].__setitem__(7, 0x2000),
            lambda r: r['owners'][0]['reply'].__setitem__(6, 0),
            lambda r: r['owners'][0]['published_code'].__setitem__(0, 0),
            lambda r: r['common']['done'].__setitem__(1, 31),
            lambda r: r['owners'][0]['trials'][0].__setitem__('completed', True),
        ]
        for index, change in enumerate(changes):
            r = report_fixture()
            change(r)
            with self.subTest(index=index), self.assertRaises(ValueError):
                oracle.audit_development(r)
        r = report_fixture()
        image = bytearray.fromhex(r['images'][0])
        image[8191] ^= 1
        r['images'][0] = image.hex()
        with self.assertRaises(ValueError):
            oracle.audit_development(r)

    def test_atomic_chain_and_lse_feature_are_required(self):
        for change in (
            lambda r: r['owners'][0].__setitem__('isar0', 0),
            lambda r: r['owners'][0]['trials'][0]['captured']['x'].__setitem__(4, r['owners'][0]['trials'][0]['token']),
        ):
            r = report_fixture(7, 4, 3)
            change(r)
            with self.assertRaises(ValueError):
                oracle.audit_development(r)
        r = report_fixture(7, 4)
        image = bytearray.fromhex(r['images'][0])
        image[4096:4100] = (99).to_bytes(4, 'little')
        r['images'][0] = image.hex()
        with self.assertRaises(ValueError):
            oracle.audit_development(r)
        for encoding in range(16):
            if encoding == 2:
                continue
            r = report_fixture(7, 1)
            r['owners'][0]['isar0'] = encoding << 20
            with self.subTest(atomic_encoding=encoding), self.assertRaises(ValueError):
                oracle.audit_development(r)

    def test_raw_fence_controls_preserve_state_without_claiming_full_lfence(self):
        for profile in (10, 11, 12):
            record = report_fixture(profile, 2, 1)
            summary = oracle.audit_development(record)
            self.assertEqual(summary['raw_fence_controls'], 64)
            self.assertEqual(summary['outcomes'], [])
            record['owners'][1]['trials'][0]['fence_completed'] = 0
            with self.assertRaises(ValueError):
                oracle.audit_development(record)

    def test_strict_acceptance_replays_complete_report_and_requested_binding(self):
        self.assertEqual(oracle.check(native_post(), expected_profile=9, expected_vcpus=4,
                                      expected_rotation=2, expected_seed=37)['native_faults'], 32)
        for kwargs in (dict(expected_profile=8), dict(expected_vcpus=3),
                       dict(expected_rotation=1), dict(expected_seed=1), dict(expected_vcpus=True)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                oracle.check(native_post(), **kwargs)
        for bad in ({}, None):
            with self.assertRaises(ValueError):
                oracle.check(bad)
        r = native_post()
        r['images'].pop()
        with self.assertRaises(ValueError):
            oracle.check(r)


if __name__ == '__main__':
    unittest.main()
